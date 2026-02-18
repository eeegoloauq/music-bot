import asyncio
import base64
import json
import logging
import os
import re
import time
from typing import Callable, Awaitable
from urllib.parse import urlparse

import aiohttp
from mutagen.flac import FLAC, Picture

logger = logging.getLogger(__name__)

INSTANCES_URL = "https://monochrome.samidy.com/instances.json"
TIDAL_API_URL = "https://api.tidal.com/v1"
TIDAL_TOKEN = "CzET4vdadNUFQ5JU"

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


async def fetch_track_url(track_id: str) -> tuple[str, dict]:
    """Returns (download_url, stream_metadata)."""
    data = await _api_get(f"/track/?id={track_id}&quality=LOSSLESS")
    manifest_b64 = data.get("manifest", "")
    manifest = json.loads(base64.b64decode(manifest_b64))
    urls = manifest.get("urls", [])
    if not urls:
        raise RuntimeError(f"No download URL in manifest for track {track_id}")
    meta = {
        "trackReplayGain": data.get("trackReplayGain"),
        "trackPeak": data.get("trackPeakAmplitude"),
        "albumReplayGain": data.get("albumReplayGain"),
        "albumPeak": data.get("albumPeakAmplitude"),
    }
    return urls[0], meta


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
                stream_meta: dict | None, cover_data: bytes | None):
    """Write metadata tags to a FLAC file. Only writes missing tags."""
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

        audio.save()
    except Exception:
        logger.warning("Could not write tags to %s", filepath)


async def download_single_track(track: dict, album_ctx: dict, dest_dir: str) -> tuple[str, bool]:
    """Download and tag a single track. Returns (filepath, was_downloaded)."""
    artist = _sanitize(album_ctx.get("artist", track["artist"]))
    album_title = _sanitize(album_ctx.get("title", "Singles"))
    album_dir = os.path.join(dest_dir, artist, album_title)
    os.makedirs(album_dir, exist_ok=True)
    os.chmod(album_dir, 0o777)
    os.chmod(os.path.join(dest_dir, artist), 0o777)

    disc = track.get("discNumber", 1)
    num = track.get("trackNumber", 0)
    track_title = _sanitize(track["title"])
    filename = f"{disc}-{num:02d} {track_title}.flac"
    filepath = os.path.join(album_dir, filename)

    if os.path.exists(filepath):
        logger.info("Track already exists: %s — %s", track["artist"], track["title"])
        return filepath, False

    cover_data = await _download_cover(album_ctx.get("cover_uuid", ""), album_dir)
    flac_url, stream_meta = await fetch_track_url(track["id"])

    t0 = time.monotonic()
    track_bytes = await _download_flac(flac_url, filepath)
    elapsed = time.monotonic() - t0
    speed = (track_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
    logger.info("Track: %s — %s | %.1fMB in %.1fs (%.1f MB/s)",
                track["artist"], track["title"], track_bytes / (1024 * 1024), elapsed, speed)
    os.chmod(filepath, 0o666)

    _write_tags(filepath, track, album_ctx, stream_meta, cover_data)
    return filepath, True


def _sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip('. ')


ProgressCallback = Callable[[int, int, str], Awaitable[None]] | None


async def download_album(
    album_id: str,
    dest_dir: str,
    progress: ProgressCallback = None,
    album: dict | None = None,
) -> dict:
    """Download album tracks, skipping existing files.

    Returns {"album_dir", "downloaded", "skipped", "failed", "total"}.
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
    logger.info("Album: %s — %s (%d tracks)", album["artist"], album["title"], total)

    for i, track in enumerate(album["tracks"], 1):
        disc = track.get("discNumber", 1)
        num = track.get("trackNumber", 0)
        track_title = _sanitize(track["title"])
        filename = f"{disc}-{num:02d} {track_title}.flac"
        filepath = os.path.join(album_dir, filename)

        if os.path.exists(filepath):
            skipped += 1
            logger.info("  [%d/%d] %s — already exists, skipping", i, total, track["title"])
            continue

        if progress:
            await progress(i, total, track["title"])

        try:
            flac_url, stream_meta = await fetch_track_url(track["id"])

            t0 = time.monotonic()
            track_bytes = await _download_flac(flac_url, filepath)
            elapsed = time.monotonic() - t0
            speed = (track_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
            logger.info("  [%d/%d] %s — %.1fMB in %.1fs (%.1f MB/s)",
                         i, total, track["title"], track_bytes / (1024 * 1024), elapsed, speed)
            album_bytes += track_bytes
            os.chmod(filepath, 0o666)

            _write_tags(filepath, track, album, stream_meta, cover_data)
            downloaded += 1
        except Exception as e:
            logger.warning("  [%d/%d] track %s (id=%s) — failed: %s",
                           i, total, track["title"], track["id"], e)
            failed.append((track["title"], str(e)))

    album_elapsed = time.monotonic() - album_t0
    album_mb = album_bytes / (1024 * 1024)
    avg_speed = album_mb / album_elapsed if album_elapsed > 0 else 0
    logger.info("Album done: %s — %s | %.0fMB, %d downloaded, %d skipped, %d failed in %.0fs (%.1f MB/s)",
                album["artist"], album["title"], album_mb, downloaded, skipped, len(failed),
                album_elapsed, avg_speed)
    return {
        "album_dir": album_dir,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "total": total,
    }
