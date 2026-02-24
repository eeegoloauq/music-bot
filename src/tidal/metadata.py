import base64
import json
import logging
import xml.etree.ElementTree as ET

import aiohttp

from config import QUALITY
from tidal.client import (
    _api_get, _get_session, _get_instances, _get_lrclib_sem,
    TIDAL_API_URL, TIDAL_TOKEN, LRCLIB_URL,
)

logger = logging.getLogger(__name__)


async def fetch_album(album_id: str) -> dict:
    data = await _api_get(f"/album/?id={album_id}")
    cover_uuid = data.get("cover", "")
    if cover_uuid:
        cover_uuid = cover_uuid.replace("-", "/")

    album_artist = data.get("artist", {}).get("name", "Unknown Artist")

    tracks = []
    for entry in data.get("items", []):
        item = entry.get("item", entry)
        artists_list = item.get("artists", [])
        main_artists = [a["name"] for a in artists_list if a.get("type") == "MAIN"]
        feat_artists = [a["name"] for a in artists_list if a.get("type") == "FEATURED"]
        track_artist = "; ".join(main_artists) if main_artists else album_artist

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
        "numberOfTracks": data.get("numberOfTracks", len(tracks)),
        "type": data.get("type", ""),
        "tracks": tracks,
    }


def _parse_mpd(mpd_xml: str) -> dict:
    """Parse DASH MPD manifest. Returns {init_url, segment_urls, codec}."""
    ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
    root = ET.fromstring(mpd_xml)
    for adapt in root.findall(".//mpd:AdaptationSet", ns):
        if "audio" not in adapt.get("contentType", "") and "audio" not in adapt.get("mimeType", ""):
            continue
        reps = adapt.findall("mpd:Representation", ns)
        if not reps:
            continue
        rep = max(reps, key=lambda r: int(r.get("bandwidth", 0)))
        codec = rep.get("codecs", "")
        st = rep.find("mpd:SegmentTemplate", ns) or adapt.find("mpd:SegmentTemplate", ns)
        if st is None:
            continue
        init_url = st.get("initialization", "")
        media_template = st.get("media", "")
        tl = st.find("mpd:SegmentTimeline", ns)
        numbers = []
        num = 1
        if tl is not None:
            for s in tl.findall("mpd:S", ns):
                repeat = int(s.get("r", 0))
                for _ in range(repeat + 1):
                    numbers.append(num)
                    num += 1
        segment_urls = [media_template.replace("$Number$", str(n)) for n in numbers]
        return {"init_url": init_url, "segment_urls": segment_urls, "codec": codec}
    raise RuntimeError("No audio adaptation set found in MPD")


async def _fetch_hires(track_id: str) -> tuple[dict, dict] | None:
    """Try HI_RES_LOSSLESS on every instance, return first FULL FLAC/DASH result or None.

    _api_get returns the first successful response from any instance, but some instances
    return PREVIEW (no Tidal Max subscription). We need to iterate all instances ourselves
    and skip the ones that return PREVIEW.
    """
    instances = await _get_instances()
    session = await _get_session()
    path = f"/track/?id={track_id}&quality=HI_RES_LOSSLESS"
    for inst in instances:
        url = f"{inst}{path}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    continue
                body = await resp.json(content_type=None)
            if isinstance(body, dict) and "detail" in body:
                continue
            data = body.get("data", body)
            manifest_b64 = data.get("manifest", "")
            if not manifest_b64:
                continue
            mime = data.get("manifestMimeType", "")
            if "dash" not in mime and "xml" not in mime:
                # Not a DASH manifest — instance returned something unexpected
                continue
            if data.get("assetPresentation") != "FULL":
                logger.debug("Instance %s: HI_RES_LOSSLESS is %s (no Max sub), skipping",
                             inst, data.get("assetPresentation"))
                continue
            mpd_xml = base64.b64decode(manifest_b64).decode()
            try:
                info = _parse_mpd(mpd_xml)
            except Exception as e:
                logger.warning("MPD parse failed on %s for track %s: %s", inst, track_id, e)
                continue
            if info["codec"] != "flac":
                logger.debug("Instance %s: HI_RES codec=%s (not flac), skipping",
                             inst, info["codec"])
                continue
            stream_meta = {
                "trackReplayGain": data.get("trackReplayGain"),
                "trackPeak": data.get("trackPeakAmplitude"),
                "albumReplayGain": data.get("albumReplayGain"),
                "albumPeak": data.get("albumPeakAmplitude"),
            }
            info["type"] = "dash"
            info["ext"] = "m4a"
            logger.info("HI_RES_LOSSLESS via %s: %d segments for track %s",
                        inst, len(info["segment_urls"]), track_id)
            return info, stream_meta
        except Exception as e:
            logger.debug("Instance %s HI_RES attempt failed: %s", inst, e)
            continue
    return None


async def fetch_track_url(track_id: str, quality: str | None = None) -> tuple[dict, dict]:
    """Fetch download info for a track.

    Returns (download_info, stream_meta).
    download_info keys:
      type: 'direct' | 'dash'
      ext:  'flac'   | 'm4a'
      url:  str                    (type=direct)
      init_url, segment_urls: ...  (type=dash)
      codec: str

    quality overrides the QUALITY env setting for this call.
    If HI_RES_LOSSLESS, tries every instance for a FULL FLAC/DASH manifest,
    falls back to LOSSLESS if none supports it.
    """
    effective_quality = quality if quality is not None else QUALITY
    if effective_quality == "HI_RES_LOSSLESS":
        result = await _fetch_hires(track_id)
        if result:
            return result
        logger.info("No instance supports HI_RES_LOSSLESS for track %s, falling back to LOSSLESS",
                    track_id)

    # LOSSLESS: standard single-URL FLAC via _api_get failover
    data = await _api_get(f"/track/?id={track_id}&quality=LOSSLESS")
    manifest_b64 = data.get("manifest", "")
    if not manifest_b64:
        raise RuntimeError(f"Empty manifest for track {track_id}")
    manifest = json.loads(base64.b64decode(manifest_b64))
    urls = manifest.get("urls", [])
    if not urls:
        raise RuntimeError(f"No URLs in LOSSLESS manifest for track {track_id}")
    stream_meta = {
        "trackReplayGain": data.get("trackReplayGain"),
        "trackPeak": data.get("trackPeakAmplitude"),
        "albumReplayGain": data.get("albumReplayGain"),
        "albumPeak": data.get("albumPeakAmplitude"),
    }
    codec = manifest.get("codecs", "flac")
    ext = "flac" if codec.lower() == "flac" else "m4a"
    return {
        "type": "direct",
        "ext": ext,
        "url": urls[0],
        "codec": codec,
    }, stream_meta


async def fetch_single_track(track_id: str) -> tuple[dict, dict]:
    """Fetch a single track's metadata via Tidal public API + parent album."""
    session = await _get_session()
    url = f"{TIDAL_API_URL}/tracks/{track_id}?countryCode=US"
    headers = {"x-tidal-token": TIDAL_TOKEN}
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Tidal API returned {resp.status} for track {track_id}")
        data = await resp.json(content_type=None)

    album_data = data.get("album", {})
    album_id = str(album_data.get("id", ""))
    album_ctx = {}
    if album_id:
        album_ctx = await fetch_album(album_id)

    artists_list = data.get("artists", [])
    main_artists = [a["name"] for a in artists_list if a.get("type") == "MAIN"]
    feat_artists = [a["name"] for a in artists_list if a.get("type") == "FEATURED"]
    album_artist = album_ctx.get("artist", "Unknown Artist")
    track_artist = "; ".join(main_artists) if main_artists else album_artist

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
    headers = {"User-Agent": "music-bot v1.1.0 (https://github.com/eeegoloauq/music-bot)"}
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
