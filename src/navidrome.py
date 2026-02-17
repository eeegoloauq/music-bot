import hashlib
import logging
import secrets
from urllib.parse import urljoin

import aiohttp

from config import NAVI_LOGIN, NAVI_PASS, NAVI_URL, NAVI_PUBLIC_URL, STREAM_BITRATE

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)
_STREAM_TIMEOUT = aiohttp.ClientTimeout(total=120)

_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(trust_env=True)
    return _session


async def close():
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


def _auth_params() -> dict[str, str]:
    salt = secrets.token_hex(8)
    token = hashlib.md5((NAVI_PASS + salt).encode()).hexdigest()
    return {
        "u": NAVI_LOGIN,
        "t": token,
        "s": salt,
        "v": "1.16.1",
        "c": "music-bot",
        "f": "json",
    }


async def _api_json(path: str, extra_params: dict | None = None) -> dict:
    """Call a Subsonic JSON endpoint. Returns subsonic-response dict or {}."""
    url = urljoin(NAVI_URL + "/", path)
    params = _auth_params()
    if extra_params:
        params.update(extra_params)
    session = await _get_session()
    async with session.get(url, params=params, timeout=_DEFAULT_TIMEOUT) as resp:
        data = await resp.json(content_type=None)
    if data is None:
        logger.warning("Navidrome returned null for %s (HTTP %s)", path, resp.status)
        return {}
    root = data.get("subsonic-response", {})
    if root.get("status") == "failed":
        err = root.get("error", {})
        logger.warning("Navidrome error on %s: [%s] %s",
                       path, err.get("code"), err.get("message"))
    return root


async def get_now_playing() -> list[dict]:
    root = await _api_json("rest/getNowPlaying")
    entries = root.get("nowPlaying", {}).get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    return [
        {
            "title": e.get("title", "Unknown"),
            "artist": e.get("artist", "Unknown"),
            "album": e.get("album", ""),
            "songId": e.get("id", ""),
            "albumId": e.get("albumId", ""),
            "coverArtId": e.get("coverArt", ""),
            "duration": e.get("duration", 0),
        }
        for e in entries
    ]


async def stream_song(song_id: str) -> tuple[bytes, str]:
    """Stream a song from Navidrome with MP3 transcoding."""
    url = urljoin(NAVI_URL + "/", "rest/stream")
    params = _auth_params()
    params["id"] = song_id
    params["format"] = "mp3"
    params["maxBitRate"] = STREAM_BITRATE
    session = await _get_session()
    async with session.get(url, params=params, timeout=_STREAM_TIMEOUT) as resp:
        content = await resp.read()
        disp = resp.headers.get("Content-Disposition", "")
        filename = "track.mp3"
        if "filename=" in disp:
            filename = disp.split("filename=")[-1].strip('" ')
            base = filename.rsplit(".", 1)[0]
            filename = f"{base}.mp3"
        return content, filename


async def get_cover_art(cover_id: str, size: int = 300) -> bytes:
    url = urljoin(NAVI_URL + "/", "rest/getCoverArt")
    params = _auth_params()
    params["id"] = cover_id
    params["size"] = str(size)
    session = await _get_session()
    async with session.get(url, params=params, timeout=_DEFAULT_TIMEOUT) as resp:
        return await resp.read()


async def start_scan() -> None:
    await _api_json("rest/startScan")


async def create_share(entry_id: str, description: str = "") -> str | None:
    """Create a Navidrome share link. Returns public URL or None."""
    if not NAVI_PUBLIC_URL:
        return None
    root = await _api_json("rest/createShare", {"id": entry_id, "description": description})
    if root.get("status") != "ok":
        return None
    shares = root.get("shares", {}).get("share", [])
    if isinstance(shares, dict):
        shares = [shares]
    if not shares:
        return None
    share_url = shares[0].get("url", "")
    if not share_url:
        return None
    # Rewrite internal URL to public
    if share_url.startswith(NAVI_URL):
        share_url = NAVI_PUBLIC_URL + share_url[len(NAVI_URL):]
    return share_url


async def search_album(artist: str, title: str) -> str | None:
    """Search for an album in Navidrome. Returns album ID or None."""
    root = await _api_json("rest/search3", {
        "query": f"{artist} {title}",
        "albumCount": "5",
        "songCount": "0",
        "artistCount": "0",
    })
    albums = root.get("searchResult3", {}).get("album", [])
    if isinstance(albums, dict):
        albums = [albums]
    # Find best match — artist and title both match (case-insensitive)
    artist_lower = artist.lower()
    title_lower = title.lower()
    for album in albums:
        if (album.get("artist", "").lower() == artist_lower
                and album.get("name", "").lower() == title_lower):
            return album.get("id")
    # Fallback: return first result if any
    if albums:
        return albums[0].get("id")
    return None
