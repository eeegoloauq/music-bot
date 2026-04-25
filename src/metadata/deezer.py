"""Deezer open API client. No auth required.

Rate limit: 50 requests / 5 seconds per IP. Plenty for our usage.
"""

import asyncio
import logging
import urllib.parse

import aiohttp

from metadata.client import DEEZER_API, _get_session

logger = logging.getLogger(__name__)


class DeezerError(Exception):
    pass


async def _get(path: str, params: dict | None = None, timeout: float = 8.0) -> dict:
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


async def find_album_id(artist: str, title: str) -> str | None:
    """Find a Deezer album ID matching ``artist`` and ``title``. Strips common
    title decorations (``[Explicit]``, ``(Deluxe)``, etc) and co-artist suffixes
    (``& Ye``, ``feat. X``) so the search matches Deezer's plainer canonical."""
    a = _clean_artist(artist)
    t = _clean_title(title)
    queries = [
        f'artist:"{a}" album:"{t}"',
        f"{a} {t}",
        t,
    ]
    artist_lower = (a or "").lower()
    for q in queries:
        try:
            hits = await search_albums(q, limit=5)
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


async def find_track_id(artist: str, title: str) -> str | None:
    a = _clean_artist(artist)
    t = _clean_title(title)
    queries = [
        f'artist:"{a}" track:"{t}"',
        f"{a} {t}",
    ]
    artist_lower = (a or "").lower()
    for q in queries:
        try:
            hits = await search_tracks(q, limit=5)
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
