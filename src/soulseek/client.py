"""slskd-api wrapper: search, parse results, enqueue download, monitor.

Two known slskd quirks handled here:
  1. completed searches accumulate and silently break ``state(includeResponses=True)``,
     so we delete stale searches before each new one
  2. ``state(includeResponses=True)`` sometimes returns an empty list even when
     ``responseCount > 0``, so we fall back to ``search_responses(id)``
All sync slskd-api calls go through ``asyncio.to_thread`` to keep the event loop free.
"""

import asyncio
import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass, field

import slskd_api

logger = logging.getLogger(__name__)


# Audio extensions whitelisted for download.
# LOSSLESS_EXTS is restricted to formats the tagger handles natively (FLAC).
# WAV/AIFF are technically lossless but the bot has no WAV/AIFF tag writer —
# they would land on disk as .flac-named files with WAV bytes inside, breaking
# Navidrome's parse. ALAC content typically uses the .m4a container so its
# files are handled via the M4A tagger when explicitly accepted.
# Anything else (.exe/.zip/.rar/.iso/...) is filtered before being scored
# or enqueued.
LOSSLESS_EXTS = {"flac"}
LOSSY_EXTS = {"mp3", "aac", "m4a", "ogg", "opus", "wma", "alac", "wav", "aiff"}
AUDIO_EXTS = LOSSLESS_EXTS | LOSSY_EXTS

# Cover art that may live alongside FLAC files inside a peer's album folder.
IMAGE_EXTS = {"jpg", "jpeg", "png", "webp"}


@dataclass
class SearchResult:
    """A single file from a slskd peer response."""

    username: str
    filename: str            # full remote path; slskd uses backslashes
    size: int
    bit_rate: int | None = None
    bit_depth: int | None = None
    sample_rate: int | None = None
    length: int | None = None  # duration in seconds
    has_free_slot: bool = False
    upload_speed: int = 0
    queue_length: int = 0
    score: float = 0.0       # filled in by scorer

    @property
    def basename(self) -> str:
        if "\\" in self.filename:
            return self.filename.rsplit("\\", 1)[-1]
        return self.filename.rsplit("/", 1)[-1]

    @property
    def directory(self) -> str:
        if "\\" in self.filename:
            return self.filename.rsplit("\\", 1)[0]
        return self.filename.rsplit("/", 1)[0] if "/" in self.filename else ""

    @property
    def extension(self) -> str:
        b = self.basename
        return b.rsplit(".", 1)[-1].lower() if "." in b else ""

    @property
    def is_lossless(self) -> bool:
        return self.extension in LOSSLESS_EXTS


@dataclass
class PeerFolder:
    """A directory on one peer holding a group of audio files (album folder candidate)."""

    username: str
    directory: str
    files: list[SearchResult] = field(default_factory=list)
    has_free_slot: bool = False
    upload_speed: int = 0
    queue_length: int = 0
    score: float = 0.0

    @property
    def lossless_count(self) -> int:
        return sum(1 for f in self.files if f.is_lossless)

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)


_client: slskd_api.SlskdClient | None = None


def _get_client() -> slskd_api.SlskdClient:
    global _client
    if _client is None:
        host = os.environ.get("SLSKD_HOST", "http://slskd:5030")
        # When slskd has SLSKD_NO_AUTH=true it ignores the X-API-Key header,
        # but slskd-api's constructor requires *some* non-empty value to pick
        # an auth path. Any string works — slskd lets the request through.
        api_key = os.environ.get("SLSKD_API_KEY") or "anonymous"
        _client = slskd_api.SlskdClient(host, api_key)
        logger.info("slskd client initialized (host=%s)", host)
    return _client


async def close():
    """Reset the cached client. slskd-api uses requests under the hood — no async to close."""
    global _client
    _client = None


# --- search ------------------------------------------------------------------

async def _cleanup_stale_searches():
    """Delete only *completed* searches. Active searches (isComplete=False) are
    left alone so concurrent callers don't kneecap each other's queries."""
    client = _get_client()
    try:
        existing = await asyncio.to_thread(client.searches.get_all)
    except Exception:
        return
    stale = [s for s in (existing or []) if s.get("isComplete")]
    if not stale:
        return
    logger.debug("Cleaning %d completed searches", len(stale))
    for s in stale:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(client.searches.delete, id=s["id"])


async def search(query: str, timeout_secs: int = 25, response_limit: int = 250) -> list[dict]:
    """Run a slskd search and return the raw peer responses.

    Polls ``state`` until either the search reports complete or the file count
    stabilises for ~6s (means peers stopped reporting). Falls back to
    ``search_responses`` if ``state(includeResponses=True)`` returns empty.
    """
    client = _get_client()
    await _cleanup_stale_searches()

    try:
        state = await asyncio.to_thread(
            client.searches.search_text,
            searchText=query,
            searchTimeout=timeout_secs * 1000,
            responseLimit=response_limit,
        )
    except Exception as e:
        logger.warning("Failed to start slskd search '%s': %s", query, e)
        return []

    search_id = state["id"]
    logger.info("Search started: id=%s query=%r", search_id, query)

    start = time.monotonic()
    last_count = 0
    stable_since: float | None = None
    min_wait = 4

    try:
        while time.monotonic() - start < timeout_secs:
            await asyncio.sleep(1.5)
            try:
                state = await asyncio.to_thread(client.searches.state, id=search_id)
            except Exception:
                logger.exception("Search poll failed for %s", search_id)
                continue

            file_count = state.get("fileCount", 0)
            resp_count = state.get("responseCount", 0)
            is_complete = state.get("isComplete", False)
            elapsed = time.monotonic() - start

            if file_count != last_count:
                last_count = file_count
                stable_since = time.monotonic()
            elif stable_since and (time.monotonic() - stable_since) > 6:
                logger.info("Search stable: %d files / %d peers", file_count, resp_count)
                break

            if is_complete and elapsed >= min_wait:
                logger.info("Search done: %d files / %d peers", file_count, resp_count)
                break
    except Exception:
        logger.exception("Polling crashed for search %s", search_id)

    # slskd 0.25.x only exposes peer responses *after* the search transitions
    # from InProgress to Completed. Polling stability is just an early-exit
    # heuristic — we still need to call stop() to force the state machine to
    # finalize, then briefly poll for isComplete before grabbing responses.
    with contextlib.suppress(Exception):
        await asyncio.to_thread(client.searches.stop, id=search_id)

    for _ in range(8):  # up to ~4s waiting for slskd to mark complete
        try:
            state = await asyncio.to_thread(client.searches.state, id=search_id)
        except Exception:
            break
        if state.get("isComplete"):
            break
        await asyncio.sleep(0.5)

    # Try the inline path first; many slskd builds drop responses there silently.
    final_state = await asyncio.to_thread(
        client.searches.state, id=search_id, includeResponses=True
    )
    responses: list[dict] = final_state.get("responses", []) or []
    if not responses and final_state.get("responseCount", 0) > 0:
        with contextlib.suppress(Exception):
            responses = await asyncio.to_thread(
                client.searches.search_responses, id=search_id
            ) or []
        logger.info("Fallback search_responses returned %d peers", len(responses))

    with contextlib.suppress(Exception):
        await asyncio.to_thread(client.searches.delete, id=search_id)

    return responses


# --- parse -------------------------------------------------------------------

_DURATION_FROM_NAME = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _flatten(responses: list[dict], lossless_only: bool = True) -> list[SearchResult]:
    """Convert slskd peer responses into flat SearchResult list, audio-extensions only."""
    allowed = LOSSLESS_EXTS if lossless_only else AUDIO_EXTS
    out: list[SearchResult] = []
    for resp in responses:
        username = resp.get("username", "")
        free = bool(resp.get("hasFreeUploadSlot", False))
        speed = int(resp.get("uploadSpeed", 0) or 0)
        queue = int(resp.get("queueLength", 0) or 0)
        for f in resp.get("files", []):
            fname: str = f.get("filename", "") or ""
            if not fname:
                continue
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            if ext not in allowed:
                continue
            out.append(SearchResult(
                username=username,
                filename=fname,
                size=int(f.get("size", 0) or 0),
                bit_rate=f.get("bitRate"),
                bit_depth=f.get("bitDepth"),
                sample_rate=f.get("sampleRate"),
                length=f.get("length"),
                has_free_slot=free,
                upload_speed=speed,
                queue_length=queue,
            ))
    return out


def parse_files(responses: list[dict], lossless_only: bool = True) -> list[SearchResult]:
    """Public: flatten search responses to SearchResult list with audio-ext filter."""
    return _flatten(responses, lossless_only=lossless_only)


def group_by_folder(results: list[SearchResult]) -> list[PeerFolder]:
    """Group results into per-(user, directory) folders for album-level matching."""
    bucket: dict[tuple[str, str], PeerFolder] = {}
    for r in results:
        key = (r.username, r.directory)
        if key not in bucket:
            bucket[key] = PeerFolder(
                username=r.username,
                directory=r.directory,
                has_free_slot=r.has_free_slot,
                upload_speed=r.upload_speed,
                queue_length=r.queue_length,
            )
        bucket[key].files.append(r)
    return list(bucket.values())


# --- enqueue + monitor ------------------------------------------------------

async def enqueue(username: str, files: list[SearchResult]) -> bool:
    """Queue files for download from a single peer. Returns True on success."""
    client = _get_client()
    payload = [{"filename": f.filename, "size": f.size} for f in files]
    try:
        await asyncio.to_thread(client.transfers.enqueue, username=username, files=payload)
        logger.info("Enqueued %d file(s) from %s", len(files), username)
        return True
    except Exception as e:
        logger.warning("Failed to enqueue from %s: %s", username, e)
        return False


def _flatten_downloads(raw: list[dict]) -> list[dict]:
    """slskd returns nested [{username, directories: [{directory, files: [...]}]}].
    Flatten to a list of file dicts each with username injected."""
    out = []
    for entry in raw or []:
        u = entry.get("username", "")
        for d in entry.get("directories", []) or []:
            for fe in d.get("files", []) or []:
                fe = dict(fe)
                fe["username"] = u
                out.append(fe)
    return out


async def get_downloads(username: str | None = None) -> list[dict]:
    client = _get_client()
    try:
        if username:
            raw = await asyncio.to_thread(client.transfers.get_downloads, username=username)
            # single-user response is also nested under directories
            raw = [raw] if raw else []
        else:
            raw = await asyncio.to_thread(client.transfers.get_all_downloads)
    except Exception as e:
        logger.debug("get_downloads failed: %s", e)
        return []
    return _flatten_downloads(raw)


def _state_is_complete(state: str) -> bool:
    s = (state or "").lower()
    return "completed" in s and "succeeded" in s


def _state_is_failed(state: str) -> bool:
    s = (state or "").lower()
    if not s:
        return False
    if "completed" in s and "succeeded" in s:
        return False
    return any(k in s for k in ("errored", "rejected", "timedout", "cancelled", "failed"))


async def wait_for_files(
    username: str,
    target_filenames: list[str],
    timeout_secs: int = 600,
    poll_interval: float = 2.0,
    progress_cb=None,
) -> dict[str, str]:
    """Poll slskd until each target filename reports completed or failed.

    ``progress_cb`` is called as ``await cb(filename, pct, state, speed_bps,
    bytes_transferred, size)`` whenever any of the visible fields change.

    Returns ``{filename: state}`` for every requested file. ``state`` is
    "Completed, Succeeded" on success or whatever slskd reports otherwise.
    """
    pending = set(target_filenames)
    states: dict[str, str] = {}
    start = time.monotonic()
    last_pct: dict[str, float] = {}

    while pending and (time.monotonic() - start) < timeout_secs:
        rows = await get_downloads(username)
        for row in rows:
            fname = row.get("filename", "")
            if fname not in pending:
                continue
            state = row.get("state", "") or ""
            pct = float(row.get("percentComplete", 0) or 0)
            speed = int(row.get("averageSpeed", 0) or 0)
            bytes_done = int(row.get("bytesTransferred", 0) or 0)
            size = int(row.get("size", 0) or 0)
            if progress_cb and pct != last_pct.get(fname, -1):
                last_pct[fname] = pct
                with contextlib.suppress(Exception):
                    await progress_cb(fname, pct, state, speed, bytes_done, size)
            if _state_is_complete(state) or _state_is_failed(state):
                states[fname] = state
                pending.discard(fname)
        await asyncio.sleep(poll_interval)

    # Anything still pending at timeout is reported as TimedOut.
    for fname in pending:
        states[fname] = "TimedOut, ClientSide"
    return states


async def cancel_download(username: str, filename: str, remove: bool = True) -> None:
    """Cancel a queued/in-progress download and optionally remove it from history."""
    client = _get_client()
    with contextlib.suppress(Exception):
        await asyncio.to_thread(
            client.transfers.cancel_download,
            username=username, id=filename, remove=remove,
        )


# --- file location -----------------------------------------------------------

def find_local_file(download_dir: str, username: str, remote_filename: str) -> str | None:
    """Locate the on-disk file slskd wrote for a completed transfer.

    slskd typically writes to ``<download_dir>/<username>/<remote_dir>/<basename>``,
    but it occasionally rewrites the directory part. We do a focused walk under
    the user dir first, then a broader scan as a last resort.
    """
    basename = remote_filename.rsplit("\\", 1)[-1] if "\\" in remote_filename \
               else remote_filename.rsplit("/", 1)[-1]

    user_dir = os.path.join(download_dir, username)
    if os.path.isdir(user_dir):
        for root, _, files in os.walk(user_dir):
            if basename in files:
                return os.path.join(root, basename)

    if os.path.isdir(download_dir):
        for root, _, files in os.walk(download_dir):
            if basename in files:
                return os.path.join(root, basename)

    return None
