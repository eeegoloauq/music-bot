import asyncio
import logging

import aiohttp

from tidal.files import _tidal_cover_url
from tidal.client import _api_get, _get_session, _get_lrclib_sem, LRCLIB_URL

logger = logging.getLogger(__name__)


def _parse_artists(artists_list: list, fallback: str = "Unknown Artist") -> tuple[str, list[str]]:
    """Extract main artist string and featured artists list from Tidal artists array."""
    main = [a["name"] for a in artists_list if a.get("type") == "MAIN"]
    feat = [a["name"] for a in artists_list if a.get("type") == "FEATURED"]
    artist = "; ".join(main) if main else fallback
    return artist, feat


async def fetch_album(album_id: str) -> dict:
    limit = 500
    data = await _api_get(f"/album/?id={album_id}&limit={limit}")
    items = list(data.get("items", []))
    total_tracks = int(data.get("numberOfTracks") or 0)
    offset = len(items)

    while total_tracks and offset < total_tracks:
        page = await _api_get(f"/album/?id={album_id}&limit={limit}&offset={offset}")
        page_items = page.get("items", [])
        if not page_items:
            break
        items.extend(page_items)
        offset += len(page_items)

    cover_uuid = data.get("cover", "")

    album_artist = data.get("artist", {}).get("name", "Unknown Artist")

    tracks = []
    for entry in items:
        item = entry.get("item", entry)
        artists_list = item.get("artists", [])
        track_artist, feat_artists = _parse_artists(artists_list, album_artist)

        tracks.append({
            "id": str(item.get("id", "")),
            "title": item.get("title", "Unknown"),
            "trackNumber": item.get("trackNumber", 0),
            "discNumber": item.get("volumeNumber", 1),
            "duration": item.get("duration", 0),
            "artist": track_artist,
            "featuredArtists": feat_artists,
            "isrc": item.get("isrc", ""),
            "copyright": item.get("copyright", ""),
            "explicit": item.get("explicit", False),
            "bpm": item.get("bpm"),
            "version": item.get("version"),
        })

    return {
        "id": str(data.get("id", album_id)),
        "title": data.get("title", "Unknown Album"),
        "artist": album_artist,
        "cover_uuid": cover_uuid,
        "releaseDate": data.get("releaseDate", ""),
        "copyright": data.get("copyright", ""),
        "upc": data.get("upc", ""),
        "numberOfVolumes": data.get("numberOfVolumes", 1),
        "numberOfTracks": total_tracks or len(tracks),
        "type": data.get("type", ""),
        "tracks": tracks,
    }


async def fetch_single_track(track_id: str) -> tuple[dict, dict]:
    """Fetch a single track's metadata via hifi-api /info/ + parent album."""
    data = await _api_get(f"/info/?id={track_id}")

    album_data = data.get("album", {})
    album_id = str(album_data.get("id", ""))
    album_ctx = {}
    if album_id:
        album_ctx = await fetch_album(album_id)

    artists_list = data.get("artists", [])
    album_artist = album_ctx.get("artist", "Unknown Artist")
    track_artist, feat_artists = _parse_artists(artists_list, album_artist)

    track_info = {
        "id": str(data.get("id", track_id)),
        "title": data.get("title", "Unknown"),
        "trackNumber": data.get("trackNumber", 0),
        "discNumber": data.get("volumeNumber", 1),
        "duration": data.get("duration", 0),
        "artist": track_artist,
        "featuredArtists": feat_artists,
        "isrc": data.get("isrc", ""),
        "copyright": data.get("copyright", ""),
        "explicit": data.get("explicit", False),
        "bpm": data.get("bpm"),
        "version": data.get("version"),
    }
    return track_info, album_ctx


async def search(query: str, album_limit: int = 3, track_limit: int = 5) -> dict:
    """Search Tidal for albums and tracks via hifi-api.

    Returns {"albums": [...], "tracks": [...]}.
    Each album: {id, title, artist, tracks, cover_url}
    Each track: {id, title, artist, album, duration, cover_url}
    """
    async def _search_albums():
        try:
            return await _api_get(f"/search/?al={query}&limit={album_limit}")
        except Exception as e:
            logger.warning("Album search failed for '%s': %s", query, e)
            return {}

    async def _search_tracks():
        try:
            return await _api_get(f"/search/?s={query}&limit={track_limit}")
        except Exception as e:
            logger.warning("Track search failed for '%s': %s", query, e)
            return {}

    al_data, tr_data = await asyncio.gather(_search_albums(), _search_tracks())

    albums = []
    for item in al_data.get("albums", {}).get("items", [])[:album_limit]:
        albums.append({
            "id": str(item["id"]),
            "title": item.get("title", "Unknown"),
            "artist": _parse_artists(item.get("artists", []))[0],
            "tracks": item.get("numberOfTracks", 0),
            "cover_url": _tidal_cover_url(item.get("cover", ""), 320),
        })

    track_items = tr_data.get("items", [])
    tracks = []
    for item in track_items[:track_limit]:
        album_data = item.get("album", {})
        tracks.append({
            "id": str(item["id"]),
            "title": item.get("title", "Unknown"),
            "artist": _parse_artists(item.get("artists", []))[0],
            "album": album_data.get("title", ""),
            "duration": item.get("duration", 0),
            "cover_url": _tidal_cover_url(album_data.get("cover", ""), 320),
        })

    return {"albums": albums, "tracks": tracks}


async def fetch_cover_url(album_id: str, size: int = 320) -> str:
    """Get Tidal cover art URL for an album via hifi-api. Returns URL or empty string."""
    try:
        data = await _api_get(f"/album/?id={album_id}")
        return _tidal_cover_url(data.get("cover", ""), size)
    except Exception:
        return ""


async def fetch_lyrics(track_name: str, artist_name: str, album_name: str,
                       duration: int) -> dict | None:
    """Fetch lyrics from lrclib.net. Returns dict with plainLyrics/syncedLyrics, or None."""
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
            async with session.get(LRCLIB_URL, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data and not data.get("instrumental"):
                        return data
    except Exception as e:
        logger.debug("Lyrics fetch failed for '%s': %s", track_name, e)
    return None
