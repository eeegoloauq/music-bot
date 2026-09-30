"""Resolve any music URL to a Deezer (type, id) pair.

Strategy (in order — first that succeeds wins):
  1. Direct Deezer URL → parse the ID, zero network
  2. Shazam → Apple Music URL preprocess
  3. Tidal URL  → scrape ``og:title``     → Deezer search
  4. Spotify URL → embed page JSON        → Deezer search
  5. Apple Music URL → iTunes Lookup API  → Deezer search
  6. YouTube / YouTube Music video → oEmbed title → Deezer search

Anything else resolves to ``None``; the bot then suggests searching by name.
Each resolver returns ``("album"|"track", deezer_id)`` or ``None``.
"""

import asyncio
import json
import logging
import re

import aiohttp

from metadata.client import _get_session
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
# Apple song URL: /song/(<slug>/)?<track_id> — also what Shazam links map to
_APPLE_SONG_RE = re.compile(r"music\.apple\.com/[a-z]{2}/song/(?:[^/?]+/)?(\d+)", re.IGNORECASE)
_YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/watch\?(?:\S*?&)?v=|youtu\.be/)([\w-]{11})", re.IGNORECASE,
)
# "(Official Music Video)", "[4K Remaster]", "(Lyrics)" and the like
_YT_TITLE_JUNK_RE = re.compile(
    r"\s*[(\[][^)\]]*\b(?:official|video|audio|lyrics?|visuali[sz]er|remaster(?:ed)?|hd|4k|mv)\b"
    r"[^)\]]*[)\]]",
    re.IGNORECASE,
)
_SHAZAM_SONG_RE = re.compile(r"shazam\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?song/(\d+)")
_SHAZAM_TRACK_RE = re.compile(r"shazam\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?track/(\d+)")
_SHAZAM_DISCOVERY_URL = "https://www.shazam.com/discovery/v5/en-US/US/web/-/track"

# og:title formats we parse:
#   Tidal   — "Artist - Album"  (sometimes "Artist & Co - Album")
def _og_meta(html: str, prop: str) -> str | None:
    """Return the ``content`` of ``<meta property="og:{prop}" content="...">``,
    or None. One helper for the og:title / og:description scrapes below."""
    m = re.search(
        rf'<meta\s+property=["\']og:{re.escape(prop)}["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    return m.group(1) if m else None


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
        logger.debug("Tidal scrape timed out for %s; falling through", url)
        return None
    except Exception as e:
        logger.debug("Tidal scrape failed for %s: %s; falling through", url, e)
        return None
    raw = _og_meta(html, "title")
    if not raw:
        return None
    raw = raw.replace("&amp;", "&").strip()
    if " - " not in raw:
        return None
    artist, _, title = raw.partition(" - ")
    artist, title = artist.strip(), title.strip()
    finder = deezer.find_album_id if typ == "album" else deezer.find_track_id
    deezer_id = await finder(artist, title)
    return (typ, deezer_id) if deezer_id else None


_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL,
)


def _spotify_embed_artist_title(html: str) -> tuple[str, str]:
    """``(artist, title)`` from a Spotify embed page, or ``("", "")``.
    Tracks list their artists; albums carry the artist in ``subtitle``."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return "", ""
    try:
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        title = (entity.get("name") or entity.get("title") or "").strip()
        artists = entity.get("artists") or []
        artist = (artists[0].get("name") if artists else None) or entity.get("subtitle") or ""
        return artist.strip(), title
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        return "", ""


async def _resolve_spotify(url: str) -> tuple[str, str] | None:
    m = _SPOTIFY_RE.search(url)
    if not m:
        return None
    typ, spotify_id = m.group(1).lower(), m.group(2)
    # open.spotify.com renders og: tags only for non-browser user agents; with
    # a browser UA like ours it serves the JS app shell and no metadata at all,
    # so the og: scrape came back empty for every link. The embed player page
    # ships the entity as JSON in __NEXT_DATA__ whatever the user agent.
    embed_url = f"https://open.spotify.com/embed/{typ}/{spotify_id}"
    session = await _get_session()
    try:
        async with session.get(
            embed_url,
            headers={"User-Agent": _BROWSER_UA, "Accept": "text/html"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                logger.warning("Spotify embed returned HTTP %d for %s", r.status, url)
                return None
            html = await r.text()
    except Exception as e:
        logger.warning("Spotify embed fetch failed: %s", e)
        return None

    artist, title = _spotify_embed_artist_title(html)
    if not (artist and title):
        logger.warning("Spotify embed: no artist/title for %s", url)
        return None
    finder = deezer.find_album_id if typ == "album" else deezer.find_track_id
    deezer_id = await finder(artist, title)
    return (typ, deezer_id) if deezer_id else None


async def _resolve_apple(url: str) -> tuple[str, str] | None:
    m = _APPLE_RE.search(url)
    song = _APPLE_SONG_RE.search(url)
    if m:
        album_id, track_id = m.group(1), m.group(2)
    elif song:
        album_id, track_id = None, song.group(1)
    else:
        return None
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


async def _resolve_youtube(url: str) -> tuple[str, str] | None:
    """A YouTube video → Deezer track. Topic channels ("Artist - Topic") carry
    the bare song title; music videos put "Artist - Song (Official Video)" in
    the title. Playlists (albums) have no artist in oEmbed and stay unresolved."""
    m = _YOUTUBE_RE.search(url)
    if not m:
        return None
    session = await _get_session()
    try:
        async with session.get(
            "https://www.youtube.com/oembed",
            params={"format": "json", "url": f"https://www.youtube.com/watch?v={m.group(1)}"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                logger.warning("YouTube oEmbed returned HTTP %d for %s", r.status, url)
                return None
            data = await r.json(content_type=None)
    except Exception as e:
        logger.warning("YouTube oEmbed failed: %s", e)
        return None
    author = (data.get("author_name") or "").strip()
    title = _YT_TITLE_JUNK_RE.sub("", data.get("title") or "").strip()
    if author.endswith(" - Topic"):
        artist = author[: -len(" - Topic")]
    elif " - " in title:
        artist, _, title = title.partition(" - ")
    elif author.endswith("VEVO"):
        # VEVO channels glue the name together: "TomWaitsVEVO" → "Tom Waits"
        artist = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", author.removesuffix("VEVO"))
    else:
        artist = author
    artist, title = artist.strip(), title.strip()
    if not (artist and title):
        return None
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

    # 3..6) platform-direct resolvers
    for resolver in (_resolve_tidal, _resolve_spotify, _resolve_apple, _resolve_youtube):
        result = await resolver(url)
        if result:
            return result
    return None
