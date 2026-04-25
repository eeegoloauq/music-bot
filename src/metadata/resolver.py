"""Resolve any music URL to a Deezer (type, id) pair.

Strategy (in order — first that succeeds wins):
  1. Direct Deezer URL → parse the ID, zero network
  2. Shazam → Apple Music URL preprocess
  3. Tidal URL  → scrape ``og:title``     → Deezer search
  4. Spotify URL → scrape ``og:title``    → Deezer search
  5. Apple Music URL → iTunes Lookup API  → Deezer search
  6. Last resort: Odesli (song.link) — covers YouTube Music, SoundCloud,
     Bandcamp, Pandora, Anghami, etc. Used here mostly for the long tail;
     Odesli's anonymous tier is rate-limited and sometimes mis-maps to
     Deezer, so we prefer platform-direct paths whenever we can.

Each resolver returns ``("album"|"track", deezer_id)`` or ``None``.
"""

import asyncio
import logging
import re

import aiohttp

from metadata.client import ODESLI_URL, _get_session
from metadata import deezer

logger = logging.getLogger(__name__)

# URL detectors
_DEEZER_ALBUM_RE = re.compile(r"deezer\.com/(?:[a-z]{2}/)?album/(\d+)", re.IGNORECASE)
_DEEZER_TRACK_RE = re.compile(r"deezer\.com/(?:[a-z]{2}/)?track/(\d+)", re.IGNORECASE)
_TIDAL_RE = re.compile(r"(?:listen\.)?tidal\.com/(?:browse/)?(album|track)/(\d+)", re.IGNORECASE)
_SPOTIFY_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]{2}/)?(album|track)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)
# Apple URL: /album/<slug>/<album_id>(?i=<track_id>)?
_APPLE_RE = re.compile(
    r"music\.apple\.com/[a-z]{2}/album/[^/]+/(\d+)(?:\?i=(\d+))?",
    re.IGNORECASE,
)
_SHAZAM_SONG_RE = re.compile(r"shazam\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?song/(\d+)")
_SHAZAM_TRACK_RE = re.compile(r"shazam\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?track/(\d+)")
_SHAZAM_DISCOVERY_URL = "https://www.shazam.com/discovery/v5/en-US/US/web/-/track"

# Tidal og:title format: "Artist - Album"  (sometimes "Artist & Co - Album")
_TIDAL_OG_TITLE_RE = re.compile(
    r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
# Spotify og:title: "Album - Album by Artist | Spotify"  /
#                   "Track - song and lyrics by Artist | Spotify"
_SPOTIFY_OG_TITLE_RE = re.compile(
    r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def _shazam_to_apple(url: str) -> str:
    m = _SHAZAM_SONG_RE.search(url)
    if m:
        return f"https://music.apple.com/us/song/{m.group(1)}"
    m = _SHAZAM_TRACK_RE.search(url)
    if m:
        shazam_id = m.group(1)
        session = await _get_session()
        try:
            async with session.get(
                f"{_SHAZAM_DISCOVERY_URL}/{shazam_id}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    for action in data.get("hub", {}).get("actions", []):
                        if action.get("type") == "applemusicplay" and action.get("id"):
                            return f"https://music.apple.com/us/song/{action['id']}"
        except Exception as e:
            logger.warning("Shazam discovery failed for %s: %s", shazam_id, e)
    return url


# --- platform-direct resolvers ---------------------------------------------

async def _resolve_tidal(url: str) -> tuple[str, str] | None:
    m = _TIDAL_RE.search(url)
    if not m:
        return None
    typ = m.group(1).lower()
    session = await _get_session()
    try:
        async with session.get(
            url,
            headers={"User-Agent": _BROWSER_UA, "Accept": "text/html"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                return None
            html = await r.text()
    except (asyncio.TimeoutError, aiohttp.ClientError):
        # Tidal sometimes times out / refuses non-browser TLS fingerprints.
        # Quiet log — the Odesli fallback at the bottom of the chain handles it.
        logger.debug("Tidal scrape timed out for %s; falling through", url)
        return None
    except Exception as e:
        logger.debug("Tidal scrape failed for %s: %s; falling through", url, e)
        return None
    om = _TIDAL_OG_TITLE_RE.search(html)
    if not om:
        return None
    raw = om.group(1).replace("&amp;", "&").strip()
    if " - " not in raw:
        return None
    artist, _, title = raw.partition(" - ")
    artist, title = artist.strip(), title.strip()
    finder = deezer.find_album_id if typ == "album" else deezer.find_track_id
    deezer_id = await finder(artist, title)
    return (typ, deezer_id) if deezer_id else None


async def _resolve_spotify(url: str) -> tuple[str, str] | None:
    m = _SPOTIFY_RE.search(url)
    if not m:
        return None
    typ = m.group(1).lower()
    # Spotify HTML page has: og:title="<Title> - Album by <Artist> | Spotify"
    # for albums, og:description="<Artist> · album · <Year> · <N> songs"
    session = await _get_session()
    try:
        async with session.get(
            url,
            headers={"User-Agent": _BROWSER_UA, "Accept": "text/html"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            html = await r.text()
    except Exception as e:
        logger.warning("Spotify scrape failed: %s", e)
        return None

    # og:description → "Artist · album · 2016 · 17 songs"
    desc_m = re.search(
        r'<meta\s+property=["\']og:description["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    title_m = _SPOTIFY_OG_TITLE_RE.search(html)
    if not title_m or not desc_m:
        return None
    title_full = title_m.group(1).strip()
    desc = desc_m.group(1).strip()
    # Title parsing: "Blonde - Album by Frank Ocean | Spotify"
    #                "Pyramids - song and lyrics by Frank Ocean | Spotify"
    title_match = re.match(
        r"^(.+?)\s*-\s*(?:Album|Single|EP|song(?:\s+and\s+lyrics)?)\s+by\s+",
        title_full, re.IGNORECASE,
    )
    title = title_match.group(1).strip() if title_match else title_full.split(" - ", 1)[0].strip()
    # Artist from description: "<Artist> · album · ..."  →  before the first " · "
    artist = desc.split(" · ", 1)[0].strip() if " · " in desc else ""
    if not (artist and title):
        return None
    finder = deezer.find_album_id if typ == "album" else deezer.find_track_id
    deezer_id = await finder(artist, title)
    return (typ, deezer_id) if deezer_id else None


async def _resolve_apple(url: str) -> tuple[str, str] | None:
    m = _APPLE_RE.search(url)
    if not m:
        return None
    album_id, track_id = m.group(1), m.group(2)
    session = await _get_session()
    target_id = track_id or album_id
    entity = "song" if track_id else "album"
    try:
        async with session.get(
            "https://itunes.apple.com/lookup",
            params={"id": target_id, "entity": entity},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
    except Exception as e:
        logger.warning("iTunes Lookup failed: %s", e)
        return None
    results = data.get("results") or []
    if not results:
        return None
    if track_id:
        # First result is the track when entity=song
        track = next((x for x in results if x.get("wrapperType") == "track"), results[0])
        artist = track.get("artistName", "")
        title = track.get("trackName", "")
        deezer_id = await deezer.find_track_id(artist, title) if artist and title else None
        return ("track", deezer_id) if deezer_id else None
    album = results[0]
    artist = album.get("artistName", "")
    title = album.get("collectionName", "")
    deezer_id = await deezer.find_album_id(artist, title) if artist and title else None
    return ("album", deezer_id) if deezer_id else None


# --- Odesli (last resort) --------------------------------------------------

async def _query_odesli(url: str) -> dict | None:
    session = await _get_session()
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json",
    }
    for attempt in (1, 2):
        try:
            async with session.get(
                ODESLI_URL,
                params={"url": url},
                headers=headers,
                timeout=aiohttp.ClientTimeout(connect=5, total=12),
            ) as resp:
                if resp.status == 429 and attempt == 1:
                    wait = resp.headers.get("Retry-After", "")
                    try:
                        delay = max(5, min(int(wait), 60))
                    except (TypeError, ValueError):
                        delay = 30
                    logger.info("Odesli 429 — backing off %ds before retry", delay)
                    await asyncio.sleep(delay)
                    continue
                if resp.status != 200:
                    logger.warning("Odesli HTTP %d for %s", resp.status, url)
                    return None
                return await resp.json(content_type=None)
        except asyncio.TimeoutError:
            logger.warning("Odesli timed out for %s", url)
            return None
        except Exception as e:
            logger.warning("Odesli request failed: %s", e)
            return None
    return None


async def _resolve_via_odesli(url: str) -> tuple[str, str] | None:
    data = await _query_odesli(url)
    if not data:
        return None
    deezer_link = (data.get("linksByPlatform") or {}).get("deezer", {}).get("url", "")
    if deezer_link:
        m = _DEEZER_ALBUM_RE.search(deezer_link)
        if m:
            return ("album", m.group(1))
        m = _DEEZER_TRACK_RE.search(deezer_link)
        if m:
            return ("track", m.group(1))
    entity = next(
        (v for v in (data.get("entitiesByUniqueId") or {}).values()
         if v.get("type") in ("album", "song")),
        None,
    )
    if not entity:
        return None
    artist = entity.get("artistName") or ""
    title = entity.get("title") or ""
    if not (artist and title):
        return None
    if entity["type"] == "album":
        deezer_id = await deezer.find_album_id(artist, title)
        return ("album", deezer_id) if deezer_id else None
    deezer_id = await deezer.find_track_id(artist, title)
    return ("track", deezer_id) if deezer_id else None


# --- public API ------------------------------------------------------------

async def resolve_link(url: str) -> tuple[str, str] | None:
    # 1) direct Deezer
    m = _DEEZER_ALBUM_RE.search(url)
    if m:
        return ("album", m.group(1))
    m = _DEEZER_TRACK_RE.search(url)
    if m:
        return ("track", m.group(1))

    # 2) Shazam → Apple Music URL
    url = await _shazam_to_apple(url)

    # 3..5) platform-direct resolvers (no Odesli needed)
    for resolver in (_resolve_tidal, _resolve_spotify, _resolve_apple):
        result = await resolver(url)
        if result:
            return result

    # 6) last-resort Odesli for everything else (YouTube Music, SoundCloud, ...)
    return await _resolve_via_odesli(url)
