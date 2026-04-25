"""Public metadata API: ``fetch_album``, ``fetch_single_track``, ``search``,
``fetch_cover_url``, ``fetch_lyrics``. Backed by Deezer's open API + lrclib.

The ``cover_uuid`` field in returned dicts is a historical name — it holds a
full Deezer CDN URL string. ``client.cover_url`` resizes it on demand for
thumbnails. ``library.files._cover_url`` keeps a passthrough branch for raw
Tidal UUIDs in case any pre-migration data flows through.
"""

import asyncio
import logging

import aiohttp

from metadata import deezer, lastfm
from metadata.client import LRCLIB_URL, _get_lrclib_sem, _get_session, cover_url as _resize_cover

logger = logging.getLogger(__name__)


# --- shape adapters --------------------------------------------------------

def _adapt_album(data: dict, full_tracks: list[dict | None] | None = None) -> dict:
    """Map a Deezer ``/album/{id}`` response to the dict shape downstream code expects."""
    summary_tracks = data.get("tracks", {}).get("data", [])
    full_tracks = full_tracks or [None] * len(summary_tracks)

    tracks: list[dict] = []
    max_disk = 1
    for i, (sum_t, full_t) in enumerate(zip(summary_tracks, full_tracks)):
        ft = full_t or {}
        disk = int(ft.get("disk_number") or 1)
        max_disk = max(max_disk, disk)
        track_artist = (sum_t.get("artist") or {}).get("name") or \
                       (data.get("artist") or {}).get("name", "Unknown Artist")
        # Deezer's `gain` is ReplayGain track gain in dB (already referenced
        # against their loudness target). None when Deezer hasn't measured it.
        gain = ft.get("gain")
        tracks.append({
            "id": str(sum_t.get("id") or ft.get("id") or ""),
            "title": ft.get("title") or sum_t.get("title", "Unknown"),
            "trackNumber": int(ft.get("track_position") or (i + 1)),
            "discNumber": disk,
            "duration": int(ft.get("duration") or sum_t.get("duration") or 0),
            "artist": track_artist,
            "featuredArtists": [],
            "isrc": ft.get("isrc") or "",
            "copyright": "",
            "explicit": bool(sum_t.get("explicit_lyrics", False) or ft.get("explicit_lyrics", False)),
            "bpm": ft.get("bpm") or None,
            "version": ft.get("title_version") or sum_t.get("title_version") or None,
            "track_gain": float(gain) if isinstance(gain, (int, float)) else None,
        })

    # Album gain — Deezer doesn't provide it; approximate as the mean of track
    # gains. ReplayGain players that use album-mode get a reasonable result.
    track_gains = [t["track_gain"] for t in tracks if t.get("track_gain") is not None]
    album_gain = (sum(track_gains) / len(track_gains)) if track_gains else None

    cover_xl = data.get("cover_xl") or data.get("cover_big") or ""
    genres = [g.get("name") for g in (data.get("genres") or {}).get("data", []) if g.get("name")]
    return {
        "id": str(data.get("id", "")),
        "title": data.get("title", "Unknown Album"),
        "artist": (data.get("artist") or {}).get("name", "Unknown Artist"),
        "cover_uuid": cover_xl,                     # full URL string
        "releaseDate": data.get("release_date", ""),
        "copyright": data.get("label", ""),
        "label": data.get("label", ""),
        "upc": data.get("upc", ""),
        "numberOfVolumes": max_disk,
        "numberOfTracks": data.get("nb_tracks") or len(tracks),
        "type": (data.get("record_type") or "album").upper(),
        "genres": genres,
        "album_gain": album_gain,
        "tracks": tracks,
    }


def _adapt_track_summary(t: dict, album_cover: str = "") -> dict:
    """Adapt a Deezer track-search hit to the inline-search dict shape."""
    return {
        "id": str(t.get("id", "")),
        "title": t.get("title", "Unknown"),
        "artist": (t.get("artist") or {}).get("name", "Unknown Artist"),
        "album": (t.get("album") or {}).get("title", ""),
        "duration": int(t.get("duration") or 0),
        "cover_url": _resize_cover(
            (t.get("album") or {}).get("cover_xl") or album_cover,
            size=320,
        ),
    }


def _adapt_album_summary(a: dict) -> dict:
    """Adapt a Deezer album-search hit for inline-search results."""
    return {
        "id": str(a.get("id", "")),
        "title": a.get("title", "Unknown"),
        "artist": (a.get("artist") or {}).get("name", "Unknown Artist"),
        "tracks": int(a.get("nb_tracks") or 0),
        "cover_url": _resize_cover(a.get("cover_xl") or a.get("cover_big") or "", size=320),
    }


# --- public functions ------------------------------------------------------

async def fetch_album(album_id: str) -> dict:
    """Fetch full album metadata by Deezer album ID. Genres include Last.fm
    community tags merged on top of Deezer's coarser labels (best-effort).
    """
    data = await deezer.get_album(album_id)
    summary_tracks = data.get("tracks", {}).get("data", [])
    if summary_tracks:
        # Enrich tracks with ISRC / disk_number / bpm via per-track endpoint.
        # Deezer's 50 req/5sec rate limit fits albums up to ~25 tracks in one
        # burst; bigger ones still fit because asyncio.gather is bounded by
        # aiohttp's connection pool default.
        async def _maybe_track(tid: str):
            try:
                return await deezer.get_track(tid)
            except Exception:
                return None
        full_tracks_task = asyncio.gather(*[_maybe_track(t["id"]) for t in summary_tracks])
    else:
        full_tracks_task = asyncio.sleep(0, result=[])

    album_artist = (data.get("artist") or {}).get("name", "") or ""
    album_title = data.get("title", "") or ""
    lastfm_task = lastfm.fetch_album_tags(album_artist, album_title)

    full_tracks, lastfm_tags = await asyncio.gather(full_tracks_task, lastfm_task)
    album = _adapt_album(data, full_tracks)

    if lastfm_tags:
        existing_lower = {g.lower() for g in album["genres"]}
        merged = list(album["genres"])
        for tag in lastfm_tags:
            if tag.lower() not in existing_lower:
                merged.append(tag)
                existing_lower.add(tag.lower())
        album["genres"] = merged

    return album


async def fetch_single_track(track_id: str) -> tuple[dict, dict]:
    """Fetch a single track + its parent album context. Mirrors old API."""
    track_data = await deezer.get_track(track_id)
    album_id = str((track_data.get("album") or {}).get("id") or "")
    album_ctx = await fetch_album(album_id) if album_id else {}

    artist_name = (track_data.get("artist") or {}).get("name") or \
                  album_ctx.get("artist", "Unknown Artist")
    track_info = {
        "id": str(track_data.get("id", track_id)),
        "title": track_data.get("title", "Unknown"),
        "trackNumber": int(track_data.get("track_position") or 0),
        "discNumber": int(track_data.get("disk_number") or 1),
        "duration": int(track_data.get("duration") or 0),
        "artist": artist_name,
        "featuredArtists": [],
        "isrc": track_data.get("isrc") or "",
        "copyright": "",
        "explicit": bool(track_data.get("explicit_lyrics", False)),
        "bpm": track_data.get("bpm") or None,
        "version": track_data.get("title_version") or None,
    }
    return track_info, album_ctx


async def search(query: str, album_limit: int = 3, track_limit: int = 5) -> dict:
    """Inline-search: Deezer search for albums + tracks. Same dict shape as old API."""
    async def _albums():
        try:
            return await deezer.search_albums(query, limit=album_limit)
        except Exception as e:
            logger.warning("Deezer album search failed for '%s': %s", query, e)
            return []

    async def _tracks():
        try:
            return await deezer.search_tracks(query, limit=track_limit)
        except Exception as e:
            logger.warning("Deezer track search failed for '%s': %s", query, e)
            return []

    al, tr = await asyncio.gather(_albums(), _tracks())
    return {
        "albums": [_adapt_album_summary(a) for a in al[:album_limit]],
        "tracks": [_adapt_track_summary(t) for t in tr[:track_limit]],
    }


async def fetch_cover_url(album_id: str, size: int = 320) -> str:
    """Get a Deezer cover URL by album ID, resized to ``size``×``size``."""
    try:
        data = await deezer.get_album(album_id)
    except Exception:
        return ""
    cover = data.get("cover_xl") or data.get("cover_big") or ""
    return _resize_cover(cover, size=size)


async def fetch_lyrics(track_name: str, artist_name: str, album_name: str,
                       duration: int) -> dict | None:
    """Lookup lyrics from lrclib.net. Unchanged from old implementation."""
    session = await _get_session()
    params = {
        "track_name": track_name,
        "artist_name": artist_name,
        "album_name": album_name,
        "duration": str(duration),
    }
    headers = {"User-Agent": "music-bot v1.8 (https://github.com/eeegoloauq/music-bot)"}
    try:
        async with _get_lrclib_sem():
            async with session.get(
                LRCLIB_URL, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data and not data.get("instrumental"):
                        return data
    except Exception as e:
        logger.debug("Lyrics fetch failed for '%s': %s", track_name, e)
    return None
