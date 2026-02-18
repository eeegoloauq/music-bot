import asyncio
import base64
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Callable, Awaitable
from urllib.parse import urlparse

import aiohttp
from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from config import QUALITY, WRITE_TAGS

logger = logging.getLogger(__name__)

INSTANCES_URL = "https://monochrome.samidy.com/instances.json"
TIDAL_API_URL = "https://api.tidal.com/v1"
TIDAL_TOKEN = "CzET4vdadNUFQ5JU"
LRCLIB_URL = "https://lrclib.net/api/get"

_AUDIO_EXTS = frozenset({".flac", ".mp3", ".m4a", ".ogg", ".opus", ".aac", ".wv", ".ape"})

_BUILTIN_INSTANCES = [
    "https://eu-central.monochrome.tf",
    "https://us-west.monochrome.tf",
    "https://arran.monochrome.tf",
    "https://api.monochrome.tf",
    "https://tidal-api.binimum.org",
    "https://monochrome-api.samidy.com",
    "https://triton.squid.wtf",
    "https://wolf.qqdl.site",
    "https://hifi-one.spotisaver.net",
    "https://hifi-two.spotisaver.net",
    "https://maus.qqdl.site",
    "https://vogel.qqdl.site",
    "https://hund.qqdl.site",
    "https://tidal.kinoplus.online",
]

_instances: list[str] = []
_instances_updated: float = 0
_INSTANCES_TTL = 1800  # refresh every 30 min

_session: aiohttp.ClientSession | None = None
_lrclib_sem: asyncio.Semaphore | None = None  # created lazily in async context


def _get_lrclib_sem() -> asyncio.Semaphore:
    global _lrclib_sem
    if _lrclib_sem is None:
        _lrclib_sem = asyncio.Semaphore(10)
    return _lrclib_sem


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


async def _refresh_instances() -> list[str]:
    """Fetch instance list from remote, fall back to builtin."""
    global _instances, _instances_updated
    try:
        session = await _get_session()
        async with session.get(INSTANCES_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None)
        fetched = [url.rstrip("/") for url in data.get("api", [])]
        if fetched:
            _instances = fetched
            _instances_updated = time.monotonic()
            logger.info("Loaded %d instances from remote", len(_instances))
            return _instances
    except Exception as e:
        logger.warning("Failed to fetch instances: %s", e)
    if not _instances:
        _instances = list(_BUILTIN_INSTANCES)
        _instances_updated = time.monotonic()
        logger.info("Using %d builtin instances", len(_instances))
    return _instances


async def _get_instances() -> list[str]:
    if not _instances or (time.monotonic() - _instances_updated) > _INSTANCES_TTL:
        await _refresh_instances()
    return _instances


async def _api_get(path: str) -> dict:
    """GET from Monochrome API with instance failover. Unwraps {"data": ...}."""
    global _instances_updated
    instances = await _get_instances()
    session = await _get_session()
    last_err = None
    for inst in instances:
        url = f"{inst}{path}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    last_err = f"HTTP {resp.status} from {inst}"
                    continue
                body = await resp.json(content_type=None)
                if isinstance(body, dict) and "detail" in body:
                    last_err = f"{inst}: {body['detail']}"
                    logger.warning("Instance %s: %s", inst, body["detail"])
                    continue
                if isinstance(body, dict) and "data" in body:
                    return body["data"]
                return body
        except Exception as e:
            last_err = f"{inst}: {e}"
            logger.warning("Instance %s failed: %s", inst, e)
            continue
    _instances_updated = 0  # force refresh on next call
    raise RuntimeError(f"All instances failed. Last error: {last_err}")


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
    return {
        "type": "direct",
        "ext": "flac",
        "url": urls[0],
        "codec": manifest.get("codecs", "flac"),
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


def _find_existing_track(album_dir: str, track: dict) -> str | None:
    """Find an existing audio file for this track in album_dir.

    Checks canonical filenames first (fast), then scans by ISRC and
    tracknumber+title to match files named by other tools/conventions.
    """
    disc = track.get("discNumber", 1)
    num = track.get("trackNumber", 0)
    title = _sanitize(track["title"])

    # Fast path: our canonical filenames (.flac for LOSSLESS, .m4a for HI_RES)
    for ext in (".flac", ".m4a"):
        canonical = os.path.join(album_dir, f"{disc}-{num:02d} {title}{ext}")
        if os.path.exists(canonical):
            return canonical

    if not os.path.isdir(album_dir):
        return None

    isrc = (track.get("isrc") or "").upper()
    track_title_norm = _normalize_title(track["title"])

    for fname in os.listdir(album_dir):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in _AUDIO_EXTS:
            continue
        fpath = os.path.join(album_dir, fname)
        try:
            if ext == ".flac":
                af = FLAC(fpath)
                file_isrc = next(iter(af.get("isrc") or []), "").upper()
                tnum = next(iter(af.get("tracknumber") or ["0"]), "0").split("/")[0].strip()
                ftitle = _normalize_title(next(iter(af.get("title") or [""]), ""))
            elif ext == ".m4a":
                af = MP4(fpath)
                isrc_key = "----:com.apple.iTunes:ISRC"
                raw = af.get(isrc_key, [b""])
                file_isrc = (raw[0].decode("utf-8", errors="ignore")
                             if isinstance(raw[0], (bytes, MP4FreeForm)) else str(raw[0])).upper()
                trkn = af.get("trkn", [(0, 0)])
                tnum = str(trkn[0][0]) if trkn else "0"
                nam = af.get("\xa9nam", [""])
                ftitle = _normalize_title(nam[0] if nam else "")
            else:
                af = MutagenFile(fpath, easy=True)
                if af is None:
                    continue
                file_isrc = next(iter(af.get("isrc") or []), "").upper()
                tnum = next(iter(af.get("tracknumber") or ["0"]), "0").split("/")[0].strip()
                ftitle = _normalize_title(next(iter(af.get("title") or [""]), ""))

            if isrc and file_isrc == isrc:
                return fpath
            if tnum == str(num) and ftitle == track_title_norm:
                return fpath
        except Exception:
            continue
    return None


def _comment_value(album_id: str) -> str:
    return (
        f"https://tidal.com/album/{album_id}"
        f" · Downloaded with github.com/eeegoloauq/music-bot"
    )


async def _patch_missing_tags(filepath: str, track: dict, album: dict) -> list[str]:
    """Add missing comment and lyrics to an existing file. Returns list of added tag names.

    Skips non-FLAC/non-M4A files.
    Skips lrclib if lyrics already present or lrclibchecked marker is set.
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".flac":
        return await _patch_flac_tags(filepath, track, album)
    elif ext == ".m4a":
        return await _patch_m4a_tags(filepath, track, album)
    return []


async def _patch_flac_tags(filepath: str, track: dict, album: dict) -> list[str]:
    added = []
    try:
        audio = FLAC(filepath)

        album_id = album.get("id", "")
        if album_id and "comment" not in audio:
            audio["comment"] = _comment_value(album_id)
            added.append("comment")

        if "lyrics" not in audio and "syncedlyrics" not in audio and "lrclibchecked" not in audio:
            lyrics = await fetch_lyrics(
                track["title"], track["artist"], album["title"], track["duration"],
            )
            if lyrics:
                if lyrics.get("syncedLyrics"):
                    audio["syncedlyrics"] = lyrics["syncedLyrics"]
                    added.append("syncedlyrics")
                if lyrics.get("plainLyrics"):
                    audio["lyrics"] = lyrics["plainLyrics"]
                    added.append("lyrics")
            else:
                audio["lrclibchecked"] = "1"

        if added:
            audio.save()
    except Exception as e:
        logger.warning("Could not patch FLAC tags for %s: %s", filepath, e)
    return added


async def _patch_m4a_tags(filepath: str, track: dict, album: dict) -> list[str]:
    added = []
    try:
        audio = MP4(filepath)

        album_id = album.get("id", "")
        if album_id and "\xa9cmt" not in audio:
            audio["\xa9cmt"] = [_comment_value(album_id)]
            added.append("comment")

        lrclib_key = "----:com.apple.iTunes:LRCLIBCHECKED"
        has_lyrics = "\xa9lyr" in audio
        has_checked = lrclib_key in audio

        if not has_lyrics and not has_checked:
            lyrics = await fetch_lyrics(
                track["title"], track["artist"], album["title"], track["duration"],
            )
            if lyrics:
                if lyrics.get("plainLyrics"):
                    audio["\xa9lyr"] = [lyrics["plainLyrics"]]
                    added.append("lyrics")
                if lyrics.get("syncedLyrics"):
                    audio["----:com.apple.iTunes:SYNCEDLYRICS"] = [
                        MP4FreeForm(lyrics["syncedLyrics"].encode("utf-8"))
                    ]
                    added.append("syncedlyrics")
            else:
                audio[lrclib_key] = [MP4FreeForm(b"1")]

        if added:
            audio.save()
    except Exception as e:
        logger.warning("Could not patch M4A tags for %s: %s", filepath, e)
    return added


async def _download_flac(url: str, filepath: str, max_retries: int = 3) -> int:
    """Download a FLAC file with retry. Writes to .tmp then renames atomically."""
    session = await _get_session()
    tmp_path = filepath + ".tmp"
    for attempt in range(1, max_retries + 1):
        try:
            track_bytes = 0
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=300)) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        f.write(chunk)
                        track_bytes += len(chunk)
            os.replace(tmp_path, filepath)
            return track_bytes
        except (aiohttp.ClientError, ConnectionError, OSError) as e:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            if attempt < max_retries:
                delay = 2 ** attempt
                logger.warning("Download attempt %d/%d failed: %s — retrying in %ds",
                               attempt, max_retries, e, delay)
                await asyncio.sleep(delay)
            else:
                logger.error("FLAC download failed after %d attempts (%s): %s",
                             max_retries, urlparse(url).netloc, e)
                raise


async def _download_dash(init_url: str, segment_urls: list[str], filepath: str) -> int:
    """Download DASH segments sequentially and concatenate into filepath atomically."""
    session = await _get_session()
    tmp_path = filepath + ".tmp"
    total_bytes = 0
    all_urls = [init_url] + segment_urls
    try:
        with open(tmp_path, "wb") as f:
            for url in all_urls:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    resp.raise_for_status()
                    chunk = await resp.read()
                    f.write(chunk)
                    total_bytes += len(chunk)
        os.replace(tmp_path, filepath)
        return total_bytes
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


async def _download_cover(cover_uuid: str, album_dir: str) -> bytes | None:
    """Download cover art if not already on disk. Returns image bytes or None."""
    if not cover_uuid:
        return None
    cover_path = os.path.join(album_dir, "cover.jpg")
    if os.path.exists(cover_path):
        with open(cover_path, "rb") as f:
            return f.read()
    cover_url = f"https://resources.tidal.com/images/{cover_uuid}/1280x1280.jpg"
    session = await _get_session()
    try:
        async with session.get(cover_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 200:
                cover_data = await resp.read()
                with open(cover_path, "wb") as f:
                    f.write(cover_data)
                os.chmod(cover_path, 0o666)
                return cover_data
    except Exception:
        logger.warning("Failed to download cover art")
    return None


def _write_tags(filepath: str, track: dict, album: dict,
                stream_meta: dict | None, cover_data: bytes | None,
                lyrics: dict | None = None):
    """Write Vorbis Comment tags to a FLAC file. Only writes missing tags."""
    try:
        audio = FLAC(filepath)
        artist_str = track.get("artist", album.get("artist", "Unknown"))
        feat = track.get("featuredArtists", [])
        if feat:
            artist_str += " feat. " + ", ".join(feat)

        title_str = track["title"]
        if track.get("version"):
            title_str += f" ({track['version']})"

        disc = track.get("discNumber", 1)
        num = track.get("trackNumber", 0)

        tags = {
            "artist": artist_str,
            "albumartist": album.get("artist", track.get("artist", "Unknown")),
            "album": album.get("title", "Singles"),
            "title": title_str,
            "tracknumber": str(num),
            "discnumber": str(disc),
            "totaltracks": str(album.get("numberOfTracks", 0)),
            "totaldiscs": str(album.get("numberOfVolumes", 1)),
            "date": album.get("releaseDate", ""),
            "copyright": track.get("copyright") or album.get("copyright", ""),
            "isrc": track.get("isrc", ""),
            "barcode": album.get("upc", ""),
        }

        album_id = album.get("id", "")
        if album_id:
            tags["comment"] = _comment_value(album_id)

        if album.get("type"):
            tags["releasetype"] = album["type"].lower()
        if track.get("bpm"):
            tags["bpm"] = str(track["bpm"])
        if track.get("explicit"):
            tags["itunesadvisory"] = "1"

        rg = stream_meta or {}
        if rg.get("trackReplayGain") is not None:
            tags["replaygain_track_gain"] = f"{rg['trackReplayGain']:.2f} dB"
        if rg.get("trackPeak") is not None:
            tags["replaygain_track_peak"] = f"{rg['trackPeak']:.6f}"
        if rg.get("albumReplayGain") is not None:
            tags["replaygain_album_gain"] = f"{rg['albumReplayGain']:.2f} dB"
        if rg.get("albumPeak") is not None:
            tags["replaygain_album_peak"] = f"{rg['albumPeak']:.6f}"

        for key, val in tags.items():
            if val and key not in audio:
                audio[key] = val

        if cover_data and not audio.pictures:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.data = cover_data
            audio.add_picture(pic)

        if lyrics:
            if lyrics.get("syncedLyrics") and "syncedlyrics" not in audio:
                audio["syncedlyrics"] = lyrics["syncedLyrics"]
            if lyrics.get("plainLyrics") and "lyrics" not in audio:
                audio["lyrics"] = lyrics["plainLyrics"]
        elif "lrclibchecked" not in audio:
            audio["lrclibchecked"] = "1"

        audio.save()
    except Exception:
        logger.warning("Could not write tags to %s", filepath)


def _write_m4a_tags(filepath: str, track: dict, album: dict,
                    stream_meta: dict | None, cover_data: bytes | None,
                    lyrics: dict | None = None):
    """Write iTunes-style tags to an M4A file. Only writes missing tags."""
    try:
        audio = MP4(filepath)

        artist_str = track.get("artist", album.get("artist", "Unknown"))
        feat = track.get("featuredArtists", [])
        if feat:
            artist_str += " feat. " + ", ".join(feat)

        title_str = track["title"]
        if track.get("version"):
            title_str += f" ({track['version']})"

        disc = track.get("discNumber", 1)
        num = track.get("trackNumber", 0)
        total_tracks = album.get("numberOfTracks", 0)
        total_discs = album.get("numberOfVolumes", 1)

        # Standard iTunes string tags
        str_tags = {
            "\xa9nam": title_str,
            "\xa9ART": artist_str,
            "aART": album.get("artist", track.get("artist", "Unknown")),
            "\xa9alb": album.get("title", "Singles"),
            "\xa9day": album.get("releaseDate", ""),
            "cprt": track.get("copyright") or album.get("copyright", ""),
        }
        album_id = album.get("id", "")
        if album_id:
            str_tags["\xa9cmt"] = _comment_value(album_id)

        for key, val in str_tags.items():
            if val and key not in audio:
                audio[key] = [val]

        if "trkn" not in audio:
            audio["trkn"] = [(num, total_tracks)]
        if "disk" not in audio:
            audio["disk"] = [(disc, total_discs)]
        if track.get("explicit") and "rtng" not in audio:
            audio["rtng"] = [1]

        # Free-form tags (----:com.apple.iTunes:NAME)
        def _ff(name: str, value: str):
            key = f"----:com.apple.iTunes:{name}"
            if key not in audio:
                audio[key] = [MP4FreeForm(value.encode("utf-8"))]

        if track.get("isrc"):
            _ff("ISRC", track["isrc"])
        if album.get("upc"):
            _ff("BARCODE", album["upc"])
        if album.get("type"):
            _ff("RELEASETYPE", album["type"].lower())
        if track.get("bpm"):
            _ff("BPM", str(track["bpm"]))

        rg = stream_meta or {}
        if rg.get("trackReplayGain") is not None:
            _ff("REPLAYGAIN_TRACK_GAIN", f"{rg['trackReplayGain']:.2f} dB")
        if rg.get("trackPeak") is not None:
            _ff("REPLAYGAIN_TRACK_PEAK", f"{rg['trackPeak']:.6f}")
        if rg.get("albumReplayGain") is not None:
            _ff("REPLAYGAIN_ALBUM_GAIN", f"{rg['albumReplayGain']:.2f} dB")
        if rg.get("albumPeak") is not None:
            _ff("REPLAYGAIN_ALBUM_PEAK", f"{rg['albumPeak']:.6f}")

        # Cover art
        if cover_data and "covr" not in audio:
            audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]

        # Lyrics
        if lyrics:
            if lyrics.get("plainLyrics") and "\xa9lyr" not in audio:
                audio["\xa9lyr"] = [lyrics["plainLyrics"]]
            if lyrics.get("syncedLyrics"):
                _ff("SYNCEDLYRICS", lyrics["syncedLyrics"])
        elif "----:com.apple.iTunes:LRCLIBCHECKED" not in audio:
            _ff("LRCLIBCHECKED", "1")

        audio.save()
    except Exception:
        logger.warning("Could not write M4A tags to %s", filepath)


async def download_single_track(track: dict, album_ctx: dict, dest_dir: str,
                                quality: str | None = None) -> tuple[str, bool, str]:
    """Download and tag a single track. Returns (filepath, was_downloaded, format_label)."""
    artist = _sanitize(album_ctx.get("artist", track["artist"]))
    album_title = _sanitize(album_ctx.get("title", "Singles"))
    album_dir = os.path.join(dest_dir, artist, album_title)
    os.makedirs(album_dir, exist_ok=True)
    os.chmod(album_dir, 0o777)
    os.chmod(os.path.join(dest_dir, artist), 0o777)

    existing = _find_existing_track(album_dir, track)
    if existing:
        added = await _patch_missing_tags(existing, track, album_ctx)
        if added:
            logger.info("Track patched: %s — %s (%s)",
                        track["artist"], track["title"], ", ".join(added))
        else:
            logger.info("Track already exists: %s — %s", track["artist"], track["title"])
        return existing, False, ""

    disc = track.get("discNumber", 1)
    num = track.get("trackNumber", 0)
    track_title = _sanitize(track["title"])

    cover_data = await _download_cover(album_ctx.get("cover_uuid", ""), album_dir)
    dl_info, stream_meta = await fetch_track_url(track["id"], quality=quality)
    filepath = os.path.join(album_dir, f"{disc}-{num:02d} {track_title}.{dl_info['ext']}")

    # Fetch lyrics concurrently with download — hides lrclib latency
    lyrics_task = asyncio.create_task(fetch_lyrics(
        track["title"], track["artist"], album_ctx.get("title", ""), track["duration"],
    ))
    t0 = time.monotonic()
    if dl_info["type"] == "dash":
        track_bytes = await _download_dash(dl_info["init_url"], dl_info["segment_urls"], filepath)
    else:
        track_bytes = await _download_flac(dl_info["url"], filepath)
    lyrics = await lyrics_task

    elapsed = time.monotonic() - t0
    speed = (track_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    logger.info("Track: %s — %s | %.1fMB in %.1fs (%.1f MB/s) [%s%s]",
                track["artist"], track["title"], track_bytes / (1024 * 1024), elapsed, speed,
                dl_info["ext"], " lyrics" if lyrics else "")
    os.chmod(filepath, 0o666)

    if WRITE_TAGS:
        if dl_info["ext"] == "m4a":
            _write_m4a_tags(filepath, track, album_ctx, stream_meta, cover_data, lyrics)
        else:
            _write_tags(filepath, track, album_ctx, stream_meta, cover_data, lyrics)
    format_label = "FLAC 24-bit" if dl_info["ext"] == "m4a" else "FLAC 16-bit"
    return filepath, True, format_label


def _sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip('. ')


def _normalize_title(title: str) -> str:
    """Normalize title for fuzzy matching across different tagging tools.

    Handles: square brackets → parens, feat. suffixes, extra whitespace.
    """
    t = title.replace("[", "(").replace("]", ")")
    t = re.sub(r"\s*\(feat\..*?\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+feat\..*$", "", t, flags=re.IGNORECASE)
    return t.strip().lower()


ProgressCallback = Callable[[int, int, str], Awaitable[None]] | None


async def download_album(
    album_id: str,
    dest_dir: str,
    progress: ProgressCallback = None,
    album: dict | None = None,
    quality: str | None = None,
) -> dict:
    """Download album tracks, skipping existing files.

    Returns {"album_dir", "downloaded", "skipped", "failed", "total", "format", "with_lyrics"}.
    """
    if album is None:
        album = await fetch_album(album_id)
    artist = _sanitize(album["artist"])
    title = _sanitize(album["title"])
    album_dir = os.path.join(dest_dir, artist, title)
    os.makedirs(album_dir, exist_ok=True)
    os.chmod(album_dir, 0o777)
    os.chmod(os.path.join(dest_dir, artist), 0o777)

    cover_data = await _download_cover(album.get("cover_uuid", ""), album_dir)

    total = len(album["tracks"])
    album_t0 = time.monotonic()
    album_bytes = 0
    downloaded = 0
    skipped = 0
    failed: list[tuple[str, str]] = []
    with_lyrics = 0
    format_label = ""
    logger.info("Album: %s — %s (%d tracks)", album["artist"], album["title"], total)

    # Phase 1: scan all tracks, split into existing vs to-download
    existing_map: dict[str, str] = {}   # track_id -> filepath
    to_download: list[tuple[int, dict]] = []

    for i, track in enumerate(album["tracks"], 1):
        existing = _find_existing_track(album_dir, track)
        if existing:
            existing_map[track["id"]] = existing
        else:
            to_download.append((i, track))

    # Phase 2: patch existing files — all lrclib calls fire in parallel
    if existing_map:
        patch_tracks = [(i, track) for i, track in enumerate(album["tracks"], 1)
                        if track["id"] in existing_map]
        patch_results = await asyncio.gather(*[
            _patch_missing_tags(existing_map[track["id"]], track, album)
            for _, track in patch_tracks
        ])
        for (i, track), added in zip(patch_tracks, patch_results):
            if added:
                logger.info("  [%d/%d] %s — patched: %s",
                            i, total, track["title"], ", ".join(added))
            else:
                logger.info("  [%d/%d] %s — already exists, skipping",
                            i, total, track["title"])
        skipped += len(existing_map)

    # Phase 3: download missing tracks
    # Fire all lrclib requests simultaneously before downloads start — all resolve in ~one
    # lrclib round-trip (~3-8s) instead of one per track sequentially.
    lyrics_tasks: dict[str, asyncio.Task] = {
        track["id"]: asyncio.create_task(fetch_lyrics(
            track["title"], track["artist"], album["title"], track["duration"],
        ))
        for _, track in to_download
    }

    for i, track in to_download:
        if progress:
            await progress(i, total, track["title"])

        disc = track.get("discNumber", 1)
        num = track.get("trackNumber", 0)
        track_title = _sanitize(track["title"])

        try:
            dl_info, stream_meta = await fetch_track_url(track["id"], quality=quality)
            if not format_label:
                format_label = "FLAC 24-bit" if dl_info["ext"] == "m4a" else "FLAC 16-bit"
            filepath = os.path.join(album_dir, f"{disc}-{num:02d} {track_title}.{dl_info['ext']}")

            t0 = time.monotonic()
            if dl_info["type"] == "dash":
                track_bytes = await _download_dash(
                    dl_info["init_url"], dl_info["segment_urls"], filepath
                )
            else:
                track_bytes = await _download_flac(dl_info["url"], filepath)
            lyrics = await lyrics_tasks[track["id"]]
            if lyrics:
                with_lyrics += 1

            elapsed = time.monotonic() - t0
            speed = (track_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
            logger.info("  [%d/%d] %s — %.1fMB in %.1fs (%.1f MB/s) [%s%s]",
                        i, total, track["title"], track_bytes / (1024 * 1024), elapsed, speed,
                        dl_info["ext"], " lyrics" if lyrics else "")
            album_bytes += track_bytes
            os.chmod(filepath, 0o666)

            if WRITE_TAGS:
                if dl_info["ext"] == "m4a":
                    _write_m4a_tags(filepath, track, album, stream_meta, cover_data, lyrics)
                else:
                    _write_tags(filepath, track, album, stream_meta, cover_data, lyrics)
            downloaded += 1
        except Exception as e:
            logger.warning("  [%d/%d] track %s (id=%s) — failed: %s",
                           i, total, track["title"], track["id"], e)
            failed.append((track["title"], str(e)))

    album_elapsed = time.monotonic() - album_t0
    album_mb = album_bytes / (1024 * 1024)
    avg_speed = album_mb / album_elapsed if album_elapsed > 0 else 0
    logger.info(
        "Album done: %s — %s | %.0fMB, %d downloaded, %d skipped, %d failed in %.0fs (%.1f MB/s)",
        album["artist"], album["title"], album_mb, downloaded, skipped, len(failed),
        album_elapsed, avg_speed,
    )
    return {
        "album_dir": album_dir,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "total": total,
        "format": format_label,
        "with_lyrics": with_lyrics,
    }
