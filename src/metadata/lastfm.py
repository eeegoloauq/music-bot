"""Last.fm community tags as supplementary genres.

Deezer's ``genres`` are coarse ("Hip Hop", "Rock"). Last.fm's user tags give
the fine-grained labels people actually search by ("witch house", "shoegaze",
"chillwave"). We merge filtered top-N tags into the album's ``genres`` list
so Navidrome surfaces them as facets.

Best-effort: if the key is missing or the request fails, we skip silently —
Last.fm tags are an enhancement, never a download blocker.
"""

import asyncio
import logging
import os
import re

import aiohttp

logger = logging.getLogger(__name__)

LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
# Last.fm rejects aiohttp's default UA with HTTP 403.
_LASTFM_HEADERS = {"User-Agent": "music-bot v1.8 (https://github.com/eeegoloauq/music-bot)"}

# Dedicated session: Last.fm needs to go through the host's HTTP_PROXY because
# the container's direct egress IP gets classified as VPN/datacenter and the
# API blocks it with HTTP 403. The other metadata APIs (Deezer, Odesli) take
# the opposite path — they throttle the shared proxy IP, so they bypass it
# via metadata.client._get_session(trust_env=False).
_lastfm_session: aiohttp.ClientSession | None = None
_lastfm_sem: asyncio.Semaphore | None = None


async def _get_lastfm_session() -> aiohttp.ClientSession:
    global _lastfm_session
    if _lastfm_session is None or _lastfm_session.closed:
        _lastfm_session = aiohttp.ClientSession(trust_env=True)
    return _lastfm_session


async def close():
    global _lastfm_session
    if _lastfm_session and not _lastfm_session.closed:
        await _lastfm_session.close()
        _lastfm_session = None

# Crowd-sourced tags are noisy. Drop the ones that don't describe the music.
_NOISE_TAGS = frozenset({
    # Subjective ratings / reactions
    "seen live", "love", "loved", "loves", "love it", "loved it",
    "5 stars", "5 star", "10/10", "perfect", "amazing", "awesome",
    "good", "great", "the best", "best", "best of", "all time",
    "all time favorites", "all time favourites",
    "peak", "fire", "ai", "goat", "goated", "banger", "bangers", "slay",
    # Catch-all noise
    "all", "no genre", "untagged", "(no genre)", "misc",
    # Platform / source tags
    "spotify", "youtube", "deezer", "tidal", "soundcloud", "bandcamp",
    "soulseek", "qobuz", "applemusic", "apple music", "amazon music",
    # Generic
    "music", "song", "songs", "track", "tracks", "album", "albums",
    "playlist", "playlists", "saved", "library", "discography",
    # Frequency / mood ambiguity
    "want to hear", "going to hear", "to listen", "to hear", "to check out",
    "need to hear", "want", "wishlist",
})

# Bare release years like "1992" / "2013" duplicate what's already in the
# DATE / YEAR tag. Decades like "1990s" / "90s" / "2000s" describe a sonic
# era and stay (think "90s rock", "70s funk").
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
# "best of …" anything: subjective annotation, not a genre.
_BEST_OF_RE = re.compile(r"^best\s+", re.IGNORECASE)
# "favourite|favorite [songs|albums|…]" — subjective, not musical.
_FAVOURITE_RE = re.compile(r"^favou?rite[s]?\b", re.IGNORECASE)


def _looks_like_keymash(tag: str) -> bool:
    """Detect random keymash spam like ``Dbdbdbdbdb`` or ``aaaaa``."""
    if len(tag) < 4:
        return False
    unique = len({c for c in tag.lower() if c.isalpha()})
    return unique < 3


def _is_noise(tag: str, artist: str, album: str) -> bool:
    t = tag.strip().lower()
    if not t or len(t) < 2:
        return True
    if t in _NOISE_TAGS:
        return True
    if _YEAR_RE.match(t):
        return True
    if _BEST_OF_RE.match(t):
        return True
    if _FAVOURITE_RE.match(t):
        return True
    if _looks_like_keymash(t):
        return True
    # Tags that are basically the artist name or album title — Last.fm users
    # often tag their library with the artist as a tag.
    if artist and t == artist.lower():
        return True
    if album and t == album.lower():
        return True
    return False


def _normalize_tag(tag: str) -> str:
    # ``str.title()`` upper-cases letters after digits, so "90s" comes back as
    # "90S". Lower-case any single trailing letter that follows a digit so
    # decade tags read naturally.
    out = tag.strip().title()
    return re.sub(r"(\d)([A-Z])(?=\b)", lambda m: m.group(1) + m.group(2).lower(), out)


def _get_sem() -> asyncio.Semaphore:
    global _lastfm_sem
    if _lastfm_sem is None:
        _lastfm_sem = asyncio.Semaphore(3)
    return _lastfm_sem


async def fetch_album_tags(
    artist: str, album: str,
    min_count: int = 5,
    top_n: int = 5,
) -> list[str]:
    """Top Last.fm community tags for an album, filtered + Title-Cased.

    Returns an empty list on any failure (no key, network error, album not
    found, all tags filtered out). ``min_count`` is the user-vote weight floor;
    Last.fm's count goes 1-100 normalised, so 5 drops the long-tail random
    noise without losing real signals.
    """
    if not LASTFM_API_KEY or not artist or not album:
        return []

    params = {
        "method": "album.getTopTags",
        "artist": artist,
        "album": album,
        "api_key": LASTFM_API_KEY,
        "format": "json",
        "autocorrect": "1",
    }
    session = await _get_lastfm_session()
    try:
        async with _get_sem():
            async with session.get(
                LASTFM_URL, params=params, headers=_LASTFM_HEADERS,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    logger.debug("Last.fm HTTP %d for %s — %s", resp.status, artist, album)
                    return []
                data = await resp.json(content_type=None)
    except Exception as e:
        logger.debug("Last.fm fetch failed for %s — %s: %s", artist, album, e)
        return []

    if "error" in data:
        # Common case: "Album not found" — silent return.
        return []

    raw = (data.get("toptags") or {}).get("tag") or []
    if isinstance(raw, dict):
        # Single-tag responses come back un-wrapped.
        raw = [raw]

    candidates: list[tuple[int, str]] = []
    for entry in raw:
        name = entry.get("name", "")
        try:
            count = int(entry.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count < min_count:
            continue
        if _is_noise(name, artist, album):
            continue
        candidates.append((count, name))

    candidates.sort(reverse=True)

    seen: set[str] = set()
    out: list[str] = []
    for _, name in candidates:
        normalized = _normalize_tag(name)
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
        if len(out) >= top_n:
            break

    return out
