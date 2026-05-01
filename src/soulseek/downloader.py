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
    _locate_existing_album, _cover_url,
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


class PeerTransferError(Exception):
    """Signals a per-peer issue we should retry the next candidate for —
    enqueue rejected, transfer stalled, file never landed in slskd's dir.
    Local I/O / tagging errors are *not* this; they propagate as their own
    exception types so a real bug doesn't get silently masked as 'try next
    peer'."""


# --- helpers ---------------------------------------------------------------

def _modal_quality(matched_files) -> tuple[int | None, int | None]:
    """Pick the dominant (bit_depth, sample_rate) across a folder's matched
    files, ignoring None positions and unreported quality. Used as the
    quality-lock target so per-track gap-fill stays uniform with the folder."""
    counts: dict[tuple[int | None, int | None], int] = {}
    for f in matched_files or []:
        if f is None:
            continue
        key = (f.bit_depth, f.sample_rate)
        if key == (None, None):
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return (None, None)
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _pick_quality_locked(picks, quality_lock):
    """From a candidate list, return the first one matching the (bd, sr) lock.
    Falls back to the first candidate if none match — better a mismatched
    track than a missing one. Quality-lock with None components matches
    anything for that axis."""
    if not picks:
        return None
    if not quality_lock or quality_lock == (None, None):
        return picks[0]
    target_bd, target_sr = quality_lock
    for p in picks:
        if (target_bd is None or p.bit_depth == target_bd) and \
           (target_sr is None or p.sample_rate == target_sr):
            return p
    chosen = picks[0]
    logger.warning(
        "Quality-lock %s not satisfied; falling back to %s/%s — album may end up mixed-quality",
        _quality_label(quality_lock),
        f"{chosen.bit_depth}-bit" if chosen.bit_depth else "?",
        f"{chosen.sample_rate // 1000}kHz" if chosen.sample_rate else "?",
    )
    return chosen


def _quality_label(qlock) -> str:
    if not qlock:
        return "any quality"
    bd, sr = qlock
    parts = []
    if bd:
        parts.append(f"{bd}-bit")
    if sr:
        parts.append(f"{sr // 1000}kHz")
    return "/".join(parts) if parts else "any quality"


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


def _prune_empty_parents(start_dir: str, stop_dir: str) -> None:
    """Remove empty slskd staging folders left after importing a file.

    slskd recreates the peer's remote directory tree under ``/downloads``.
    After we move the finished file into the library, those folders are often
    empty. Prune upward, but never remove the staging root itself.
    """
    current = os.path.abspath(start_dir)
    stop = os.path.abspath(stop_dir)
    if current == stop or not current.startswith(stop + os.sep):
        return
    while current != stop:
        try:
            os.rmdir(current)
        except OSError:
            break
        current = os.path.dirname(current)


def _remove_staging_traces(username: str, remote_filename: str) -> None:
    """Best-effort removal of partial / orphaned files from a failed transfer.

    Cleans both the main download tree (where slskd writes the completed
    file) and slskd's ``.incomplete/`` sidecar (where in-flight bytes live),
    then prunes any now-empty parent dirs back up to the staging root. All
    failures are swallowed — this is a janitor pass, not a correctness path.
    """
    basename = remote_filename.rsplit("\\", 1)[-1] if "\\" in remote_filename \
               else remote_filename.rsplit("/", 1)[-1]

    candidates: list[str] = []
    located = slskd.find_local_file(SLSKD_DOWNLOAD_DIR, username, remote_filename)
    if located:
        candidates.append(located)
    incomplete_root = os.path.join(SLSKD_DOWNLOAD_DIR, ".incomplete")
    if os.path.isdir(incomplete_root):
        for root, _, files in os.walk(incomplete_root):
            if basename in files:
                candidates.append(os.path.join(root, basename))

    for path in candidates:
        with contextlib.suppress(OSError):
            os.remove(path)
        _prune_empty_parents(os.path.dirname(path), SLSKD_DOWNLOAD_DIR)


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

    Returns ``(filepath, bytes_transferred, fmt_label)``. Raises
    ``PeerTransferError`` for per-peer issues (caller retries another peer);
    other exceptions (OSError on move, mutagen on tag) propagate as-is.
    """
    ok = await slskd.enqueue(chosen.username, [chosen])
    if not ok:
        raise PeerTransferError(f"slskd refused enqueue from {chosen.username}")

    state = await _await_one_file(
        chosen.username, chosen.filename, on_progress=on_progress,
    )
    if "succeeded" not in state.lower():
        with contextlib.suppress(Exception):
            await slskd.cancel_download(chosen.username, chosen.filename)
        _remove_staging_traces(chosen.username, chosen.filename)
        raise PeerTransferError(f"transfer ended in state '{state}'")

    src_path = slskd.find_local_file(SLSKD_DOWNLOAD_DIR, chosen.username, chosen.filename)
    if not src_path or not os.path.isfile(src_path):
        _remove_staging_traces(chosen.username, chosen.filename)
        raise PeerTransferError(f"could not locate downloaded file for {chosen.basename}")

    disc = track.get("discNumber", 1)
    num = track.get("trackNumber", 0)
    track_title = _sanitize(track["title"])
    prefix = _track_prefix(disc, num, album.get("numberOfVolumes", 1))
    ext = chosen.extension if chosen.extension in ("flac", "m4a", "mp3") else "flac"
    dest_path = os.path.join(album_dir, f"{prefix} {track_title}.{ext}")

    src_dir = os.path.dirname(src_path)
    _move_into_library(src_path, dest_path)
    _prune_empty_parents(src_dir, SLSKD_DOWNLOAD_DIR)
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
    """Try peers in order; return on first success. Only ``PeerTransferError``
    is treated as 'try the next peer' — any other exception (local I/O,
    tagging, etc.) is a real bug and propagates so it doesn't get silently
    swallowed as a peer issue."""
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
        except PeerTransferError as e:
            last_err = str(e)
            logger.warning("candidate %s failed for %s: %s",
                           cand.username, track.get("title"), e)
            continue
    if last_err:
        raise PeerTransferError(last_err)
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
    existing_album_dir = _locate_existing_album(dest_dir, album_ctx)
    if existing_album_dir is not None:
        album_dir = existing_album_dir
    else:
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

    # Tag-based dedup: identity lives in the audio files' `comment` (our
    # Deezer-ID anchor) and `album` tags, not in the folder name. Folders
    # sanitised by some other tool (`:` → space vs `:` → `_`, etc.) match
    # too — name-of-folder is presentation, not identity.
    existing_album_dir = _locate_existing_album(dest_dir, album)
    if existing_album_dir is not None:
        album_dir = existing_album_dir
    else:
        album_dir = _ensure_album_dir(dest_dir, _sanitize(album["artist"]),
                                       _sanitize(album["title"]))

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
    # When the chosen peer rejects K tracks in a row we abandon it for the
    # next folder-rank alternative (a single peer with bad share/queue state
    # otherwise eats the whole album).
    track_provenance: dict[str, str] = {}
    folder_match, folder_alternatives = await find_album(album)

    # Folder-match strategy
    #   complete coverage (missing_count == 0): prefer it whole. Frankenstein
    #     assemblies are strictly worse than a single peer.
    #   partial coverage (≥75%): use what the folder covers, fill gaps
    #     per-track. The per-track searches are filtered to match the
    #     folder's modal bit-depth/sample-rate so the album stays uniform.
    #   below 75%: skip folder, full per-track. The fallback's job is to
    #     pick a coherent assembly from across peers.
    PARTIAL_COVERAGE_MIN = 0.75
    # K consecutive failures from one peer → that peer is systemically broken
    # (their slskd queue / share index / DB). Abandon it, retry the rest from
    # the next-best folder candidate. K=1 over-reacts to single transient
    # rejections; K=3+ wastes minutes per dead peer. Stall-timeouts (peer
    # accepts then never delivers) still cost SLSKD_DOWNLOAD_TIMEOUT * K
    # before we give up — known limitation, separate fix.
    PEER_ABANDON_K = 2

    n_tracks = len(album["tracks"])
    folder_chain: list[ScoredFolder] = []
    quality_lock: tuple[int | None, int | None] | None = None
    if folder_match:
        coverage = (n_tracks - folder_match.missing_count) / n_tracks if n_tracks else 0
        if coverage >= PARTIAL_COVERAGE_MIN:
            folder_chain = [folder_match] + list(folder_alternatives or [])
            quality_lock = _modal_quality(folder_match.matched_files)
            label = "Album-folder match" if folder_match.missing_count == 0 \
                else f"Partial-folder match (gap-fill {folder_match.missing_count}/{n_tracks})"
            alt_note = f" (+{len(folder_alternatives)} fallback peers)" \
                if folder_alternatives else ""
            logger.info("%s: peer=%s score=%.1f covers %d/%d tracks @ %s%s",
                        label, folder_match.folder.username, folder_match.score,
                        n_tracks - folder_match.missing_count, n_tracks,
                        _quality_label(quality_lock), alt_note)

    # Prefetch lyrics in parallel; the per-track download awaits its own task.
    lyrics_tasks: dict[str, asyncio.Task] = {
        t["id"]: asyncio.create_task(fetch_lyrics(
            t["title"], t["artist"], album["title"], t.get("duration", 0),
        ))
        for _, t in to_download
    }

    # Stable per-track index for the bot's progress UI — same value across
    # retries from different peers.
    download_total = len(to_download)
    progress_idx_by_id = {t["id"]: idx for idx, (_, t) in enumerate(to_download, 1)}
    remaining_track_ids: set[str] = {t["id"] for _, t in to_download}
    last_error: dict[str, str] = {}

    async def _attempt(i: int, track: dict, candidates: list[SearchResult],
                       ) -> SearchResult | None:
        """Try ``candidates`` for ``track`` in order. Updates album-level
        bookkeeping (downloaded count, byte total, format label, lyrics flag,
        provenance) on success. Returns the SearchResult that succeeded, or
        ``None`` on failure (with ``last_error`` updated)."""
        nonlocal album_bytes, downloaded, format_label, with_lyrics
        progress_idx = progress_idx_by_id[track["id"]]

        async def _on_track_progress(pct: float, speed_bps: int, eta_sec: int,
                                      _i=i, _title=track["title"]):
            if progress:
                with contextlib.suppress(Exception):
                    await progress(
                        _i, total, progress_idx, download_total, _title,
                        {"pct": pct, "speed_bps": speed_bps, "eta_sec": eta_sec},
                    )

        # Initial heartbeat — slskd may stall a few seconds before the first
        # transfer-progress event fires, so the user sees movement immediately.
        if progress:
            with contextlib.suppress(Exception):
                await progress(i, total, progress_idx, download_total,
                               track["title"], None)

        try:
            t0 = time.monotonic()
            res = await _try_candidates(
                candidates, track, album, album_dir, cover_data,
                lyrics_tasks.get(track["id"]),
                on_progress=_on_track_progress,
                max_attempts=max(len(candidates), 1),
            )
            if res is None:
                raise PeerTransferError("no peer returned a usable file")
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
            track_provenance[track["id"]] = chosen.username
            remaining_track_ids.discard(track["id"])
            last_error.pop(track["id"], None)
            with contextlib.suppress(Exception):
                if lyrics_tasks[track["id"]].done() and lyrics_tasks[track["id"]].result():
                    with_lyrics += 1
            return chosen
        except PeerTransferError as e:
            last_error[track["id"]] = str(e)
            logger.warning("  [%d/%d] track %s — failed: %s",
                           i, total, track["title"], e)
            return None

    # === Folder-chain phase ===
    # Walk ranked folder peers, abandoning any that rack up K consecutive
    # rejections. Successes from earlier peers stay; only the unfilled tail
    # moves to the next peer.
    for folder_rank, folder in enumerate(folder_chain):
        if not remaining_track_ids:
            break
        folder_chosen: dict[str, SearchResult] = {}
        for tr, sr in zip(album["tracks"], folder.matched_files):
            if tr["id"] in remaining_track_ids and sr is not None:
                folder_chosen[tr["id"]] = sr
        if not folder_chosen:
            continue
        if folder_rank > 0:
            logger.info(
                "Folder fallback #%d: peer=%s score=%.1f covers %d remaining track(s)",
                folder_rank, folder.folder.username, folder.score, len(folder_chosen),
            )
        failures_in_a_row = 0
        for i, track in to_download:
            if track["id"] not in folder_chosen:
                continue
            if track["id"] not in remaining_track_ids:
                continue
            used = await _attempt(i, track, [folder_chosen[track["id"]]])
            if used is not None:
                failures_in_a_row = 0
            else:
                failures_in_a_row += 1
                if failures_in_a_row >= PEER_ABANDON_K:
                    logger.warning(
                        "Abandoning peer %s after %d consecutive failures, switching to next folder",
                        folder.folder.username, failures_in_a_row,
                    )
                    break

    # === Per-track fallback phase ===
    # Tracks that no folder covered — or whose folder chain failed entirely.
    # Single-track searches accept Frankenstein assembly across peers, with
    # the modal quality_lock keeping the album uniform where possible.
    if remaining_track_ids:
        needs_per_track = [(i, t) for (i, t) in to_download
                           if t["id"] in remaining_track_ids]
        logger.info("Per-track search for %d remaining track(s)", len(needs_per_track))
        per_track_results = await find_tracks_concurrent(
            [t for _, t in needs_per_track],
            album_artist=album["artist"],
        )
        for (i, track), (auto, alts) in zip(needs_per_track, per_track_results):
            picks = [auto.result] if auto else []
            picks += [a.result for a in alts if (auto is None or a is not auto)]
            chosen = _pick_quality_locked(picks, quality_lock) if picks else None
            if chosen is None:
                last_error.setdefault(track["id"], "no Soulseek match")
                continue
            track_alts_local = [r for r in picks if r is not chosen]
            candidates = [chosen] + track_alts_local
            used = await _attempt(i, track, candidates)
            if used is not None and quality_lock is None:
                quality_lock = (used.bit_depth, used.sample_rate)

    # Cancel orphaned lyrics tasks for tracks we never managed to download.
    for tid in remaining_track_ids:
        with contextlib.suppress(Exception):
            if not lyrics_tasks[tid].done():
                lyrics_tasks[tid].cancel()

    # Tally final failures from whatever's still unfilled.
    for _, track in to_download:
        if track["id"] in remaining_track_ids:
            failed.append((track["title"],
                           last_error.get(track["id"], "no Soulseek match")))

    elapsed = time.monotonic() - album_t0
    mb = album_bytes / (1024 * 1024)
    avg_speed = mb / elapsed if elapsed > 0 else 0
    logger.info(
        "Album done: %s — %s | %.0fMB, %d downloaded, %d skipped, %d failed in %.0fs (%.1f MB/s)",
        album["artist"], album["title"], mb, downloaded, skipped, len(failed),
        elapsed, avg_speed,
    )
    if track_provenance:
        logger.info("Sources: %s", _provenance_summary(track_provenance))

    return _result_dict(album_dir, downloaded, skipped, failed, total,
                        format_label, with_lyrics)


def _provenance_summary(track_provenance: dict[str, str]) -> str:
    """One-line summary of which peers supplied which tracks. ``5 from FatCJ
    + 1 from xtdeck`` for a 6-track assembly."""
    counts: dict[str, int] = {}
    for peer in track_provenance.values():
        counts[peer] = counts.get(peer, 0) + 1
    parts = [f"{n} from {peer}" for peer, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    return " + ".join(parts)


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
