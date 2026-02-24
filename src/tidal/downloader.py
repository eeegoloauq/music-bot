import asyncio
import logging
import os
import time
from typing import Callable, Awaitable
from urllib.parse import urlparse

import aiohttp

from config import QUALITY, WRITE_TAGS
from tidal.client import _get_session
from tidal.metadata import fetch_album, fetch_track_url, fetch_lyrics
from tidal.files import _find_existing_track, _sanitize, _track_prefix
from tidal.tagger import _write_tags, _write_m4a_tags, _patch_missing_tags

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], Awaitable[None]] | None


async def _download_flac(url: str, filepath: str, max_retries: int = 3) -> int:
    """Download a FLAC file with retry. Writes to .tmp then renames atomically."""
    session = await _get_session()
    tmp_path = filepath + ".tmp"
    for attempt in range(1, max_retries + 1):
        try:
            track_bytes = 0
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        f.write(chunk)
                        track_bytes += len(chunk)
            os.replace(tmp_path, filepath)
            return track_bytes
        except (aiohttp.ClientError, ConnectionError, OSError) as e:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            if attempt < max_retries:
                delay = 2 ** attempt
                logger.warning("Download attempt %d/%d failed: %s — retrying in %ds",
                               attempt, max_retries, e, delay)
                await asyncio.sleep(delay)
            else:
                logger.error("FLAC download failed after %d attempts (%s): %s",
                             max_retries, urlparse(url).netloc, e)
                raise


async def _download_dash(init_url: str, segment_urls: list[str], filepath: str) -> int:
    """Download DASH segments sequentially and concatenate into filepath atomically."""
    session = await _get_session()
    tmp_path = filepath + ".tmp"
    total_bytes = 0
    all_urls = [init_url] + segment_urls
    try:
        with open(tmp_path, "wb") as f:
            for url in all_urls:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    resp.raise_for_status()
                    chunk = await resp.read()
                    f.write(chunk)
                    total_bytes += len(chunk)
        os.replace(tmp_path, filepath)
        return total_bytes
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


async def _remux_to_flac(m4a_path: str) -> str:
    """Remux FLAC-in-M4A (DASH) to a proper FLAC container via ffmpeg.

    Copies the audio stream without re-encoding — lossless and fast.
    Returns the new .flac path and removes the original .m4a.
    """
    flac_path = m4a_path[:-4] + ".flac"
    tmp_path = flac_path + ".tmp"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", m4a_path, "-c:a", "copy", "-f", "flac", "-y", tmp_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg remux failed for {m4a_path}")
    os.replace(tmp_path, flac_path)
    os.remove(m4a_path)
    return flac_path


def _write_audio_tags(filepath: str, dl_info: dict, track: dict, album: dict,
                      stream_meta: dict, cover_data: bytes | None, lyrics: dict | None):
    if dl_info["ext"] == "m4a":
        _write_m4a_tags(filepath, track, album, stream_meta, cover_data, lyrics)
    else:
        _write_tags(filepath, track, album, stream_meta, cover_data, lyrics)


def _fmt_label(dl_info: dict) -> str:
    if dl_info["type"] == "dash":
        return "FLAC 24-bit"
    codec = dl_info.get("codec", "flac").lower()
    if codec == "flac":
        return "FLAC 16-bit"
    if "alac" in codec:
        return "ALAC"
    return "M4A"


async def _download_cover(cover_uuid: str, album_dir: str) -> bytes | None:
    """Download cover art if not already on disk. Returns image bytes or None."""
    if not cover_uuid:
        return None
    cover_path = os.path.join(album_dir, "cover.jpg")
    if os.path.exists(cover_path):
        with open(cover_path, "rb") as f:
            return f.read()
    cover_url = f"https://resources.tidal.com/images/{cover_uuid}/1280x1280.jpg"
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


async def download_single_track(track: dict, album_ctx: dict, dest_dir: str,
                                quality: str | None = None) -> tuple[str, bool, str]:
    """Download and tag a single track. Returns (filepath, was_downloaded, format_label)."""
    artist = _sanitize(album_ctx.get("artist", track["artist"]))
    album_title = _sanitize(album_ctx.get("title", "Singles"))
    album_dir = os.path.join(dest_dir, artist, album_title)
    os.makedirs(album_dir, exist_ok=True)
    os.chmod(album_dir, 0o777)
    os.chmod(os.path.join(dest_dir, artist), 0o777)

    existing = _find_existing_track(album_dir, track)
    if existing:
        added = await _patch_missing_tags(existing, track, album_ctx)
        if added:
            logger.info("Track patched: %s — %s (%s)",
                        track["artist"], track["title"], ", ".join(added))
        else:
            logger.info("Track already exists: %s — %s", track["artist"], track["title"])
        return existing, False, ""

    disc = track.get("discNumber", 1)
    num = track.get("trackNumber", 0)
    total_discs = album_ctx.get("numberOfVolumes", 1)
    track_title = _sanitize(track["title"])

    cover_data = await _download_cover(album_ctx.get("cover_uuid", ""), album_dir)
    dl_info, stream_meta = await fetch_track_url(track["id"], quality=quality)
    prefix = _track_prefix(disc, num, total_discs)
    filepath = os.path.join(album_dir, f"{prefix} {track_title}.{dl_info['ext']}")

    # Fetch lyrics concurrently with download — hides lrclib latency
    lyrics_task = asyncio.create_task(fetch_lyrics(
        track["title"], track["artist"], album_ctx.get("title", ""), track["duration"],
    ))
    t0 = time.monotonic()
    if dl_info["type"] == "dash":
        track_bytes = await _download_dash(dl_info["init_url"], dl_info["segment_urls"], filepath)
        filepath = await _remux_to_flac(filepath)
    else:
        track_bytes = await _download_flac(dl_info["url"], filepath)
    lyrics = await lyrics_task

    elapsed = time.monotonic() - t0
    speed = (track_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    fmt = _fmt_label(dl_info)
    logger.info("Track: %s — %s | %.1fMB in %.1fs (%.1f MB/s) [%s%s]",
                track["artist"], track["title"], track_bytes / (1024 * 1024), elapsed, speed,
                fmt, " lyrics" if lyrics else " no lyrics")
    os.chmod(filepath, 0o666)

    if WRITE_TAGS:
        _write_audio_tags(filepath, dl_info, track, album_ctx, stream_meta, cover_data, lyrics)
    return filepath, True, fmt


async def download_album(
    album_id: str,
    dest_dir: str,
    progress: ProgressCallback = None,
    album: dict | None = None,
    quality: str | None = None,
) -> dict:
    """Download album tracks, skipping existing files.

    Returns {"album_dir", "downloaded", "skipped", "failed", "total", "format", "with_lyrics"}.
    """
    if album is None:
        album = await fetch_album(album_id)
    artist = _sanitize(album["artist"])
    title = _sanitize(album["title"])
    album_dir = os.path.join(dest_dir, artist, title)
    os.makedirs(album_dir, exist_ok=True)
    os.chmod(album_dir, 0o777)
    os.chmod(os.path.join(dest_dir, artist), 0o777)

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

    # Phase 1: scan all tracks, split into existing vs to-download
    existing_map: dict[str, str] = {}   # track_id -> filepath
    to_download: list[tuple[int, dict]] = []

    for i, track in enumerate(album["tracks"], 1):
        existing = _find_existing_track(album_dir, track)
        if existing:
            existing_map[track["id"]] = existing
        else:
            to_download.append((i, track))

    # Phase 2: patch existing files — all lrclib calls fire in parallel
    if existing_map:
        patch_tracks = [(i, track) for i, track in enumerate(album["tracks"], 1)
                        if track["id"] in existing_map]
        patch_results = await asyncio.gather(*[
            _patch_missing_tags(existing_map[track["id"]], track, album)
            for _, track in patch_tracks
        ])
        for (i, track), added in zip(patch_tracks, patch_results):
            if added:
                logger.info("  [%d/%d] %s — patched: %s",
                            i, total, track["title"], ", ".join(added))
            else:
                logger.info("  [%d/%d] %s — already exists, skipping",
                            i, total, track["title"])
        skipped += len(existing_map)

    # Phase 3: download missing tracks
    # Fire all lrclib requests simultaneously before downloads start — all resolve in ~one
    # lrclib round-trip (~3-8s) instead of one per track sequentially.
    lyrics_tasks: dict[str, asyncio.Task] = {
        track["id"]: asyncio.create_task(fetch_lyrics(
            track["title"], track["artist"], album["title"], track["duration"],
        ))
        for _, track in to_download
    }

    for i, track in to_download:
        if progress:
            await progress(i, total, track["title"])

        disc = track.get("discNumber", 1)
        num = track.get("trackNumber", 0)
        track_title = _sanitize(track["title"])

        try:
            dl_info, stream_meta = await fetch_track_url(track["id"], quality=quality)
            prefix = _track_prefix(disc, num, album.get("numberOfVolumes", 1))
            filepath = os.path.join(album_dir, f"{prefix} {track_title}.{dl_info['ext']}")

            t0 = time.monotonic()
            if dl_info["type"] == "dash":
                track_bytes = await _download_dash(
                    dl_info["init_url"], dl_info["segment_urls"], filepath
                )
                filepath = await _remux_to_flac(filepath)
            else:
                track_bytes = await _download_flac(dl_info["url"], filepath)
            lyrics = await lyrics_tasks[track["id"]]
            if lyrics:
                with_lyrics += 1

            elapsed = time.monotonic() - t0
            speed = (track_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
            fmt = _fmt_label(dl_info)
            if not format_label:
                format_label = fmt
            logger.info("  [%d/%d] %s — %.1fMB in %.1fs (%.1f MB/s) [%s%s]",
                        i, total, track["title"], track_bytes / (1024 * 1024), elapsed, speed,
                        fmt, " lyrics" if lyrics else " no lyrics")
            album_bytes += track_bytes
            os.chmod(filepath, 0o666)

            if WRITE_TAGS:
                _write_audio_tags(filepath, dl_info, track, album, stream_meta, cover_data, lyrics)
            downloaded += 1
        except Exception as e:
            logger.warning("  [%d/%d] track %s (id=%s) — failed: %s",
                           i, total, track["title"], track["id"], e)
            failed.append((track["title"], str(e)))

    album_elapsed = time.monotonic() - album_t0
    album_mb = album_bytes / (1024 * 1024)
    avg_speed = album_mb / album_elapsed if album_elapsed > 0 else 0
    logger.info(
        "Album done: %s — %s | %.0fMB, %d downloaded, %d skipped, %d failed in %.0fs (%.1f MB/s)",
        album["artist"], album["title"], album_mb, downloaded, skipped, len(failed),
        album_elapsed, avg_speed,
    )
    return {
        "album_dir": album_dir,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "total": total,
        "format": format_label,
        "with_lyrics": with_lyrics,
    }
