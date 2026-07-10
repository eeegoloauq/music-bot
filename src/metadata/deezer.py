"""Deezer open API client. No auth required.

Rate limit: 50 requests / 5 seconds per IP. The rolling-window limiter below
keeps any caller (notably the re-tagger, which bursts albums × tracks back to
back) under the cap even when individual call sites use ``asyncio.gather``.
"""

import asyncio
import logging
import time

import aiohttp

from metadata.client import DEEZER_API, _get_session

logger = logging.getLogger(__name__)


class DeezerError(Exception):
    pass


class _RateLimiter:
    """Async rolling-window limiter: at most ``max_per_window`` requests
    inside any ``window_secs`` interval. Pending callers FIFO-block on the
    lock until the oldest tracked timestamp falls out of the window.
    """

    def __init__(self, max_per_window: int, window_secs: float):
        self._max = max_per_window
        self._window = window_secs
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            cutoff = now - self._window
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            if len(self._timestamps) >= self._max:
                wait = self._timestamps[0] + self._window - now
                if wait > 0:
                    await asyncio.sleep(wait)
                now = time.monotonic()
                cutoff = now - self._window
                self._timestamps = [t for t in self._timestamps if t > cutoff]
            self._timestamps.append(now)


# 40 / 5s leaves 20% headroom under Deezer's documented 50 / 5s cap, so
# bursts inside fetch_album (1 + N parallel track requests) coexist with
# ongoing re-tagger work without tripping the "Quota limit exceeded" guard.
_rate_limiter = _RateLimiter(max_per_window=40, window_secs=5.0)


async def _get(path: str, params: dict | None = None, timeout: float = 8.0) -> dict:
    await _rate_limiter.acquire()
    session = await _get_session()
    url = f"{DEEZER_API}{path}"
    # Force English genre/category names — without this header Deezer returns
    # localised strings based on the requesting IP, which varies by deploy host.
    headers = {"Accept-Language": "en-US,en;q=0.9"}
    async with session.get(
        url,
        params=params,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        if resp.status != 200:
            raise DeezerError(f"HTTP {resp.status} from {url}")
        data = await resp.json(content_type=None)
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise DeezerError(f"Deezer API error: {err.get('message', err)}")
    return data


# --- albums ---------------------------------------------------------------

async def get_album(album_id: str) -> dict:
    """Full album metadata + summary track list."""
    return await _get(f"/album/{album_id}")


async def get_track(track_id: str) -> dict:
    """Full track metadata including ISRC, disk_number, track_position."""
    return await _get(f"/track/{track_id}")


async def get_track_by_isrc(isrc: str) -> dict:
    """Direct track lookup by ISRC (used by the local-upload identify ladder)."""
    return await _get(f"/track/isrc:{isrc}")


async def get_album_by_upc(upc: str) -> dict:
    """Direct album lookup by UPC/barcode."""
    return await _get(f"/album/upc:{upc}")


# --- search ---------------------------------------------------------------

async def search_albums(query: str, limit: int = 5) -> list[dict]:
    """Search albums by free-text query, returns top ``limit`` hits."""
    if not query:
        return []
    data = await _get("/search/album", params={"q": query, "limit": limit})
    return data.get("data", [])


async def search_tracks(query: str, limit: int = 5) -> list[dict]:
    """Search tracks by free-text query."""
    if not query:
        return []
    data = await _get("/search/track", params={"q": query, "limit": limit})
    return data.get("data", [])


import re as _re
import unicodedata as _ud


def _norm_words(s: str) -> set[str]:
    s = _ud.normalize("NFKD", s or "")
    s = "".join(c for c in s if not _ud.combining(c))
    return set(_re.findall(r"\w+", s.lower()))


def _artist_word_overlap(want: str, got: str) -> bool:
    """True iff at least one ≥3-char word from `want` appears in `got`."""
    want_words = {w for w in _norm_words(want) if len(w) >= 3}
    if not want_words:
        return True
    return bool(want_words & _norm_words(got))


# Decorations that platforms add to titles which Deezer doesn't echo back —
# e.g. "BULLY [Explicit]" on Tidal is just "BULLY" on Deezer. Strip them
# before search or the strict format finds zero results.
_TITLE_DECOR_RE = _re.compile(
    r"\s*[\(\[][^)\]]*?\b(?:explicit|clean|deluxe|expanded|anniversary|"
    r"special|bonus[^)\]]*|remaster(?:ed)?|version|edition|mix)[^)\]]*[\)\]]",
    _re.IGNORECASE,
)
_FEAT_RE = _re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+.+$", _re.IGNORECASE)
_AMP_COARTIST_RE = _re.compile(r"\s+&\s+.+$")  # only "& X" (preserve "Tyler, The Creator")


def _clean_artist(s: str) -> str:
    s = _FEAT_RE.sub("", s or "")
    s = _AMP_COARTIST_RE.sub("", s)
    return s.strip()


def _clean_title(s: str) -> str:
    s = _TITLE_DECOR_RE.sub("", s or "")
    s = _FEAT_RE.sub("", s)
    return s.strip()


async def _find_id(artist: str, title: str, search_fn, key: str,
                   title_only_fallback: bool) -> str | None:
    """Search Deezer for an ``artist``/``title`` match, returning its id.

    Strips title decorations (``[Explicit]``, ``(Deluxe)``) and co-artist
    suffixes (``& Ye``, ``feat. X``) to match Deezer's plainer canonical, then
    tries progressively looser queries. Prefers an exact artist-name match,
    falling back to word-overlap. ``key`` is ``album``/``track`` (the Deezer
    advanced-search field); ``title_only_fallback`` adds a bare-title query —
    fine for albums (distinctive) but too noisy for tracks."""
    a = _clean_artist(artist)
    t = _clean_title(title)
    queries = [f'artist:"{a}" {key}:"{t}"', f"{a} {t}"]
    if title_only_fallback:
        queries.append(t)
    artist_lower = (a or "").lower()
    for q in queries:
        try:
            hits = await search_fn(q, limit=5)
        except DeezerError:
            continue
        if not hits:
            continue
        for h in hits:
            if h.get("artist", {}).get("name", "").lower() == artist_lower:
                return str(h["id"])
        for h in hits:
            if _artist_word_overlap(a, h.get("artist", {}).get("name", "")):
                return str(h["id"])
    return None


async def find_album_id(artist: str, title: str) -> str | None:
    return await _find_id(artist, title, search_albums, "album", True)


async def find_track_id(artist: str, title: str) -> str | None:
    return await _find_id(artist, title, search_tracks, "track", False)
