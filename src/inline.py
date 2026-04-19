import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from io import BytesIO
from uuid import uuid4

from telegram import (
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InlineQueryResultsButton,
    InputTextMessageContent,
    Update,
)
from telegram.ext import ContextTypes

from config import ALLOWED_USERS, MUSIC_DIR
import tidal
import navidrome

logger = logging.getLogger(__name__)

# Bounded caches — evict oldest when full
_CACHE_MAX = 2000

# song_id -> telegram file_id
_file_id_cache: OrderedDict[str, str] = OrderedDict()
# song_id -> asyncio.Event (signals upload completion)
_upload_events: dict[str, asyncio.Event] = {}
# songs that failed upload — skip retries for 1 hour
_upload_failed: dict[str, float] = {}  # song_id -> monotonic timestamp
_UPLOAD_FAILED_TTL = 3600  # seconds

_TG_MAX_AUDIO_BYTES = 50 * 1024 * 1024  # Telegram bot upload limit
# entry_id -> share URL
_share_url_cache: OrderedDict[str, str] = OrderedDict()


def _cache_set(cache: OrderedDict, key: str, value) -> None:
    """Set a value in a bounded OrderedDict cache."""
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _CACHE_MAX:
        cache.popitem(last=False)


_DELETE_PREFIX = "delete:"

_AUDIO_EXTS = frozenset({".flac", ".mp3", ".m4a", ".ogg", ".opus", ".aac", ".wv", ".ape"})
_TIDAL_ALBUM_RE = re.compile(r"tidal\.com/album/(\d+)")

# Cache now-playing to avoid repeated Navidrome calls during rapid inline re-queries
_np_cache: list[dict] = []
_np_cache_time: float = 0
_NP_CACHE_TTL = 2  # seconds



def _find_tidal_album_id_in_dir(album_dir: str) -> str:
    """Find Tidal album ID from any audio file's comment tag in a directory."""
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4

    if not os.path.isdir(album_dir):
        return ""
    try:
        for f in os.scandir(album_dir):
            if not f.is_file():
                continue
            ext = os.path.splitext(f.name)[1].lower()
            try:
                if ext == ".flac":
                    comment = next(iter(FLAC(f.path).get("comment") or []), "")
                elif ext == ".m4a":
                    comment = next(iter(MP4(f.path).get("\xa9cmt") or []), "")
                else:
                    continue
                m = _TIDAL_ALBUM_RE.search(comment)
                if m:
                    return m.group(1)
            except Exception:
                continue
    except OSError:
        pass
    return ""


def _read_lyrics_from_file(filepath: str) -> str | None:
    """Read plain or synced lyrics from a FLAC/M4A file."""
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4

    try:
        ext = os.path.splitext(filepath)[1].lower()
        if ext == ".flac":
            tags = FLAC(filepath)
            for key in ("lyrics", "unsyncedlyrics"):
                val = tags.get(key)
                if val:
                    return val[0]
        elif ext == ".m4a":
            tags = MP4(filepath)
            val = tags.get("\xa9lyr")
            if val:
                return val[0]
    except Exception:
        pass
    return None


async def _get_now_playing_cached() -> list[dict] | None:
    """Get now-playing from cache or Navidrome. Returns list or None on error."""
    global _np_cache, _np_cache_time
    if _np_cache and (time.monotonic() - _np_cache_time) < _NP_CACHE_TTL:
        return _np_cache
    try:
        playing = await navidrome.get_now_playing()
        _np_cache = playing
        _np_cache_time = time.monotonic()
        return playing
    except Exception:
        return None


def _search_local_albums(query: str, limit: int = 5) -> list[dict]:
    """Search local music library for albums matching query.

    Returns list of {artist, album, path, tracks, tidal_album_id}.
    """
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4

    query_lower = query.lower()
    results = []
    try:
        for artist_entry in os.scandir(MUSIC_DIR):
            if not artist_entry.is_dir() or artist_entry.name.startswith((".", "lost")):
                continue
            for album_entry in os.scandir(artist_entry.path):
                if not album_entry.is_dir():
                    continue
                combined = f"{artist_entry.name} {album_entry.name}".lower()
                if query_lower in combined:
                    track_count = 0
                    tidal_album_id = ""
                    for f in os.scandir(album_entry.path):
                        if not f.is_file():
                            continue
                        ext = os.path.splitext(f.name)[1].lower()
                        if ext in _AUDIO_EXTS:
                            track_count += 1
                            if not tidal_album_id:
                                try:
                                    if ext == ".flac":
                                        comment = next(iter(FLAC(f.path).get("comment") or []), "")
                                    elif ext == ".m4a":
                                        comment = next(iter(MP4(f.path).get("\xa9cmt") or []), "")
                                    else:
                                        comment = ""
                                    m = _TIDAL_ALBUM_RE.search(comment)
                                    if m:
                                        tidal_album_id = m.group(1)
                                except Exception:
                                    pass
                    results.append({
                        "artist": artist_entry.name,
                        "album": album_entry.name,
                        "path": album_entry.path,
                        "tracks": track_count,
                        "tidal_album_id": tidal_album_id,
                    })
                    if len(results) >= limit:
                        return results
    except OSError:
        pass
    return results


async def _get_share_url(entry_id: str, description: str) -> str | None:
    """Get or create a share URL for an entry, with caching."""
    if entry_id in _share_url_cache:
        return _share_url_cache[entry_id]
    try:
        url = await navidrome.create_share(entry_id, description)
        if url:
            _cache_set(_share_url_cache, entry_id, url)
        return url
    except navidrome.NavidromeAuthError:
        logger.warning("Navidrome share: wrong credentials")
        return None
    except Exception as e:
        logger.warning("Failed to create share link for %s: %s", entry_id, e)
        return None



async def _ensure_cached(bot, user_id: int, entry: dict) -> str | None:
    """Make sure a song is uploaded to Telegram. Returns file_id or None."""
    song_id = entry["songId"]

    if song_id in _file_id_cache:
        logger.info("Inline: %s — %s (cached)", entry["artist"], entry["title"])
        return _file_id_cache[song_id]

    now = time.monotonic()
    if song_id in _upload_failed:
        if (now - _upload_failed[song_id]) < _UPLOAD_FAILED_TTL:
            return None
        del _upload_failed[song_id]
    # Prune expired entries periodically (keep dict bounded)
    if len(_upload_failed) > 200:
        expired = [k for k, t in _upload_failed.items() if (now - t) >= _UPLOAD_FAILED_TTL]
        for k in expired:
            del _upload_failed[k]

    # another query already uploading this song — wait for it
    if song_id in _upload_events:
        try:
            await asyncio.wait_for(_upload_events[song_id].wait(), timeout=25)
        except asyncio.TimeoutError:
            return None
        return _file_id_cache.get(song_id)

    # we're the first — do the upload
    event = asyncio.Event()
    try:
        _upload_events[song_id] = event
        t0 = time.monotonic()
        suffix = entry.get("suffix", "")
        local_path = os.path.join(MUSIC_DIR, entry.get("path", "")) if entry.get("path") else ""
        cover_coro = navidrome.get_cover_art(entry["coverArtId"]) if entry.get("coverArtId") else asyncio.sleep(0)

        if suffix in ("m4a", "mp3") and local_path and os.path.exists(local_path):
            audio_coro = navidrome.download_song(song_id, suffix)
        else:
            audio_coro = navidrome.stream_song(song_id)

        audio_result, cover_result = await asyncio.gather(
            audio_coro, cover_coro, return_exceptions=True,
        )
        if isinstance(audio_result, BaseException):
            raise audio_result
        audio_data, filename = audio_result
        t1 = time.monotonic()
        size_mb = len(audio_data) / (1024 * 1024)
        dl_speed = size_mb / (t1 - t0) if (t1 - t0) > 0 else 0
        logger.info("Inline: %s — %s | stream %.1fMB in %.1fs (%.1f MB/s)",
                     entry["artist"], entry["title"], size_mb, t1 - t0, dl_speed)

        if len(audio_data) > _TG_MAX_AUDIO_BYTES:
            logger.warning("Inline: %s — %s | too large for Telegram (%dMB), skipping",
                           entry["artist"], entry["title"], len(audio_data) // (1024 * 1024))
            _upload_failed[song_id] = time.monotonic()
            return None

        audio_io = BytesIO(audio_data)
        audio_io.name = filename
        thumb_io = None
        if isinstance(cover_result, bytes) and cover_result:
            thumb_io = BytesIO(cover_result)
            thumb_io.name = "cover.jpg"

        msg = await bot.send_audio(
            chat_id=user_id,
            audio=audio_io,
            title=entry["title"],
            performer=entry["artist"],
            thumbnail=thumb_io,
            duration=entry.get("duration") or None,
            disable_notification=True,
        )
        if msg.audio:
            _cache_set(_file_id_cache, song_id, msg.audio.file_id)
        else:
            logger.error("Upload resulted in non-Audio type for %s — %s (id=%s)",
                         entry["artist"], entry["title"], song_id)
            await msg.delete()
            return None
        await msg.delete()
        t2 = time.monotonic()
        up_speed = size_mb / (t2 - t1) if (t2 - t1) > 0 else 0
        logger.info("Inline: %s — %s | upload %.1fs (%.1f MB/s) | total %.1fs",
                     entry["artist"], entry["title"], t2 - t1, up_speed, t2 - t0)
        return _file_id_cache[song_id]
    except Exception as e:
        logger.warning("Upload failed for %s — %s (id=%s): %s",
                       entry["artist"], entry["title"], song_id, e)
        _upload_failed[song_id] = time.monotonic()
        return None
    finally:
        event.set()
        _upload_events.pop(song_id, None)


async def _inline_hint(update: Update):
    """Show help button above empty results for empty/short queries."""
    await update.inline_query.answer(
        [],
        cache_time=30,
        is_personal=True,
        button=InlineQueryResultsButton(
            text="np  s  l  lib  del — or type to search",
            start_parameter="help",
        ),
    )


async def _fetch_tidal_covers(tidal_ids: list[tuple[str, str]]) -> dict[str, str]:
    """Batch-fetch Tidal cover URLs. Takes [(key, tidal_album_id), ...], returns {key: url}."""
    if not tidal_ids:
        return {}
    covers = await asyncio.gather(*[
        tidal.fetch_cover_url(aid) for _, aid in tidal_ids
    ], return_exceptions=True)
    result = {}
    for (key, _), url in zip(tidal_ids, covers):
        if isinstance(url, str) and url:
            result[key] = url
    return result


def _album_result(title: str, artist: str, tracks: int,
                  cover_url: str | None, message: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=str(uuid4()),
        title=f"{title} ({tracks} tracks)",
        description=artist,
        thumbnail_url=cover_url or None,
        input_message_content=InputTextMessageContent(message),
    )


def _track_result(title: str, artist: str, album: str, duration: int,
                  cover_url: str | None, message: str) -> InlineQueryResultArticle:
    mins, secs = divmod(duration, 60)
    return InlineQueryResultArticle(
        id=str(uuid4()),
        title=title,
        description=f"{artist} — {album} · {mins}:{secs:02d}",
        thumbnail_url=cover_url or None,
        input_message_content=InputTextMessageContent(message),
    )


async def _inline_delete(update: Update, del_query: str):
    """Search local library and show albums for deletion."""
    if len(del_query) < 2:
        await update.inline_query.answer([], cache_time=5, is_personal=True)
        return
    local = await asyncio.to_thread(_search_local_albums, del_query)
    if not local:
        await update.inline_query.answer([], cache_time=5, is_personal=True)
        return

    cover_urls = await _fetch_tidal_covers([
        (item["path"], item["tidal_album_id"])
        for item in local if item["tidal_album_id"]
    ])

    results = []
    for item in local:
        rel_path = os.path.relpath(item["path"], MUSIC_DIR)
        results.append(_album_result(
            item["album"], item["artist"], item["tracks"],
            cover_urls.get(item["path"]),
            f"{_DELETE_PREFIX}{rel_path}",
        ))
    await update.inline_query.answer(results, cache_time=5, is_personal=True)


async def _inline_search(update: Update, query: str):
    """Search Tidal for albums and tracks with pagination."""
    try:
        offset = int(update.inline_query.offset or "0")
    except ValueError:
        offset = 0
    page_albums = 3
    page_tracks = 5
    page_size = page_albums + page_tracks

    try:
        search_data = await tidal.search(
            query,
            album_limit=page_albums + offset,
            track_limit=page_tracks + offset,
        )
    except Exception as e:
        logger.warning("Tidal search failed in inline: %s", e)
        search_data = {"albums": [], "tracks": []}

    all_albums = search_data["albums"]
    all_tracks = search_data["tracks"]
    albums = all_albums[offset:offset + page_albums]
    tracks = all_tracks[offset:offset + page_tracks]

    results = []
    for a in albums:
        results.append(_album_result(
            a["title"], a["artist"], a["tracks"],
            a["cover_url"], f"https://tidal.com/album/{a['id']}",
        ))
    for t in tracks:
        results.append(_track_result(
            t["title"], t["artist"], t["album"], t["duration"],
            t["cover_url"], f"https://tidal.com/track/{t['id']}",
        ))

    # Signal more results available if we got a full page
    next_offset = ""
    if len(albums) >= page_albums and len(tracks) >= page_tracks:
        next_offset = str(offset + page_size)

    await update.inline_query.answer(
        results, cache_time=30, next_offset=next_offset, is_personal=True,
    )


async def _inline_lyrics(update: Update, playing: list[dict]):
    """Show lyrics for the currently playing track."""
    entry = playing[0]
    lyrics_text = None

    # Try reading from local file first
    if entry.get("path"):
        local_path = os.path.join(MUSIC_DIR, entry["path"])
        if os.path.exists(local_path):
            lyrics_text = await asyncio.to_thread(_read_lyrics_from_file, local_path)

    # Fallback to lrclib
    if not lyrics_text:
        try:
            data = await tidal.fetch_lyrics(
                entry["title"], entry["artist"], entry.get("album", ""), entry.get("duration", 0),
            )
            if data:
                lyrics_text = data.get("plainLyrics") or data.get("syncedLyrics")
        except Exception:
            pass

    if lyrics_text:
        # Strip LRC timestamps for display
        plain = re.sub(r"^\[\d+:\d+\.\d+\]\s*", "", lyrics_text, flags=re.MULTILINE).strip()
        # Telegram inline message content limit is 4096 chars
        if len(plain) > 4000:
            plain = plain[:4000] + "\n..."
        await update.inline_query.answer([
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=f"Lyrics: {entry['title']}",
                description=f"{entry['artist']} — {plain[:100]}",
                input_message_content=InputTextMessageContent(
                    f"{entry['artist']} — {entry['title']}\n\n{plain}",
                ),
            )
        ], cache_time=10, is_personal=True)
    else:
        await update.inline_query.answer([], cache_time=10, is_personal=True)


async def _inline_lib_search(update: Update, query: str):
    """Search Navidrome library and return results with share links and Tidal covers."""
    try:
        search_result = await navidrome.search(query)
    except Exception as e:
        logger.warning("Navidrome library search failed: %s", e)
        await update.inline_query.answer([], cache_time=5, is_personal=True)
        return

    albums = [a for a in (search_result.get("album") or []) if isinstance(a, dict)]
    songs = [s for s in (search_result.get("song") or []) if isinstance(s, dict)]

    # Extract Tidal album IDs from song comments for cover art
    tidal_ids: dict[str, str] = {}  # navidrome albumId -> tidal album id
    for song in songs:
        album_id = song.get("albumId", "")
        if album_id and album_id not in tidal_ids:
            comment = song.get("comment", "")
            m = _TIDAL_ALBUM_RE.search(comment) if comment else None
            if m:
                tidal_ids[album_id] = m.group(1)

    cover_urls = await _fetch_tidal_covers(list(tidal_ids.items()))

    results = []
    for album in albums:
        nd_id = album.get("id", "")
        share_url = await _get_share_url(nd_id, f"{album.get('artist', '')} — {album.get('name', '')}")
        content = share_url or f"{album.get('artist', '')} — {album.get('name', '')}"
        results.append(_album_result(
            album.get("name", "Unknown"), album.get("artist", ""),
            album.get("songCount", 0), cover_urls.get(nd_id),
            content,
        ))

    for song in songs:
        song_id = song.get("id", "")
        share_url = await _get_share_url(song_id, f"{song.get('artist', '')} — {song.get('title', '')}")
        content = share_url or f"{song.get('artist', '')} — {song.get('title', '')}"
        results.append(_track_result(
            song.get("title", "Unknown"), song.get("artist", ""),
            song.get("album", ""), song.get("duration", 0),
            cover_urls.get(song.get("albumId", "")), content,
        ))

    await update.inline_query.answer(results, cache_time=15, is_personal=True)


async def _inline_share(update: Update, playing: list[dict]):
    """Send share links for now-playing tracks with Tidal cover art."""
    entry = playing[0]
    desc = f"{entry['artist']} — {entry['title']}"
    share_url = await _get_share_url(entry["songId"], desc)
    if not share_url:
        await update.inline_query.answer([], cache_time=5, is_personal=True)
        return

    # Get Tidal cover URL: find any audio file in the album dir, read comment tag
    cover_url = None
    if entry.get("path"):
        # path from Navidrome may not match disk filename, but parent dir is reliable
        album_dir = os.path.join(MUSIC_DIR, os.path.dirname(entry["path"]))
        album_id = await asyncio.to_thread(_find_tidal_album_id_in_dir, album_dir)
        if album_id:
            try:
                cover_url = await tidal.fetch_cover_url(album_id) or None
            except Exception:
                pass

    result = InlineQueryResultArticle(
        id=str(uuid4()),
        title=entry["title"],
        description=f"{entry['artist']} — {entry['album']}",
        thumbnail_url=cover_url,
        thumbnail_width=320,
        thumbnail_height=320,
        input_message_content=InputTextMessageContent(
            f"{entry['artist']} — {entry['title']}\n{share_url}",
        ),
    )
    logger.info("Share inline: cover_url=%s", cover_url)
    await update.inline_query.answer([result], cache_time=5, is_personal=True)


async def _inline_now_playing(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               playing: list[dict]):
    """Stream and send now-playing tracks as cached audio."""
    user_id = update.effective_user.id
    entry = playing[0]
    song_id = entry["songId"]

    # Already cached — return instantly
    if song_id in _file_id_cache:
        await update.inline_query.answer([
            InlineQueryResultCachedAudio(
                id=str(uuid4()),
                audio_file_id=_file_id_cache[song_id],
            )
        ], cache_time=5, is_personal=True)
        return

    # Upload synchronously — Telegram allows ~30s, upload takes 3-5s
    file_id = await _ensure_cached(context.bot, user_id, entry)
    if file_id:
        await update.inline_query.answer([
            InlineQueryResultCachedAudio(
                id=str(uuid4()),
                audio_file_id=file_id,
            )
        ], cache_time=5, is_personal=True)
    else:
        await update.inline_query.answer([], cache_time=5, is_personal=True)


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in ALLOWED_USERS:
        return

    query = (update.inline_query.query or "").strip()
    query_lower = query.lower()

    # Prefix modes (space-delimited, checked first)
    if query_lower == "del" or query_lower.startswith("del "):
        return await _inline_delete(update, query[3:].strip())

    if query_lower.startswith("lib "):
        lib_query = query[4:].strip()
        if len(lib_query) >= 2:
            return await _inline_lib_search(update, lib_query)
        return await update.inline_query.answer([], cache_time=3, is_personal=True)

    # Short exact keywords (1-2 chars, never collide with 3+ char search)
    if query_lower == "np":
        return await _np_or_share_or_lyrics(update, context, "np")
    if query_lower == "s":
        return await _np_or_share_or_lyrics(update, context, "share")
    if query_lower == "l":
        return await _np_or_share_or_lyrics(update, context, "lyrics")

    # 3+ chars = Tidal search (standard inline bot behavior)
    if len(query) >= 3:
        return await _inline_search(update, query)

    # Empty or 1-2 chars with no keyword match = hint
    return await _inline_hint(update)


async def _np_or_share_or_lyrics(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    """Dispatch np/share/lyrics modes — all need Navidrome now-playing data."""
    playing = await _get_now_playing_cached()
    if not playing:
        return await update.inline_query.answer([], cache_time=3, is_personal=True)
    playing = playing[:1]

    if mode == "share":
        return await _inline_share(update, playing)
    if mode == "lyrics":
        return await _inline_lyrics(update, playing)
    return await _inline_now_playing(update, context, playing)
