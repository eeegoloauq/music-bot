import asyncio
import logging
import os
import re
import time
from io import BytesIO
from uuid import uuid4

from telegram import (
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InputTextMessageContent,
    Update,
)
from telegram.ext import ContextTypes

from config import ALLOWED_USERS, MUSIC_DIR
import tidal
import navidrome

logger = logging.getLogger(__name__)

# song_id -> telegram file_id
_file_id_cache: dict[str, str] = {}
# song_id -> asyncio.Event (signals upload completion)
_upload_events: dict[str, asyncio.Event] = {}
# songs that failed upload (too large, unsupported, etc.) — skip retries this session
_upload_failed: set[str] = set()

_TG_MAX_AUDIO_BYTES = 50 * 1024 * 1024  # Telegram bot upload limit
# entry_id -> share URL
_share_url_cache: dict[str, str] = {}

_DELETE_PREFIX = "delete:"

_AUDIO_EXTS = frozenset({".flac", ".mp3", ".m4a", ".ogg", ".opus", ".aac", ".wv", ".ape"})
_TIDAL_ALBUM_RE = re.compile(r"tidal\.com/album/(\d+)")

# Cache now-playing to avoid repeated Navidrome calls during rapid inline re-queries
_np_cache: list[dict] = []
_np_cache_time: float = 0
_NP_CACHE_TTL = 5  # seconds


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
            _share_url_cache[entry_id] = url
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

    if song_id in _upload_failed:
        return None

    # another query already uploading this song — wait for it
    if song_id in _upload_events:
        try:
            await asyncio.wait_for(_upload_events[song_id].wait(), timeout=25)
        except asyncio.TimeoutError:
            return None
        return _file_id_cache.get(song_id)

    # we're the first — do the upload
    event = asyncio.Event()
    _upload_events[song_id] = event
    try:
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
            _upload_failed.add(song_id)
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
            _file_id_cache[song_id] = msg.audio.file_id
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
        _upload_failed.add(song_id)
        return None
    finally:
        event.set()
        _upload_events.pop(song_id, None)


async def _inline_hint(update: Update):
    """Show usage hint for empty/short queries."""
    await update.inline_query.answer([
        InlineQueryResultArticle(
            id=str(uuid4()),
            title="Search Tidal",
            description="np = now playing, s = share, del = delete",
            input_message_content=InputTextMessageContent("..."),
        )
    ], cache_time=3)


async def _inline_delete(update: Update, del_query: str):
    """Search local library and show albums for deletion."""
    if len(del_query) < 2:
        await update.inline_query.answer([], cache_time=5)
        return
    local = await asyncio.to_thread(_search_local_albums, del_query)
    if local:
        cover_urls = {}
        album_ids = [(item["path"], item["tidal_album_id"])
                     for item in local if item["tidal_album_id"]]
        if album_ids:
            covers = await asyncio.gather(*[
                tidal.fetch_cover_url(aid) for _, aid in album_ids
            ], return_exceptions=True)
            for (path, _), url in zip(album_ids, covers):
                if isinstance(url, str) and url:
                    cover_urls[path] = url

        results = []
        for item in local:
            rel_path = os.path.relpath(item["path"], MUSIC_DIR)
            results.append(
                InlineQueryResultArticle(
                    id=str(uuid4()),
                    title=f"{item['album']} ({item['tracks']} tracks)",
                    description=item["artist"],
                    thumbnail_url=cover_urls.get(item["path"]) or None,
                    input_message_content=InputTextMessageContent(
                        f"{_DELETE_PREFIX}{rel_path}",
                    ),
                )
            )
        await update.inline_query.answer(results, cache_time=5)
    else:
        await update.inline_query.answer([
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="No albums found",
                description=f"Nothing matching '{del_query}' in library",
                input_message_content=InputTextMessageContent("..."),
            )
        ], cache_time=5)


async def _inline_search(update: Update, query: str):
    """Search Tidal for albums and tracks."""
    try:
        search_data = await tidal.search(query, album_limit=3, track_limit=5)
    except Exception as e:
        logger.warning("Tidal search failed in inline: %s", e)
        search_data = {"albums": [], "tracks": []}

    results = []
    for a in search_data["albums"]:
        results.append(
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=f"{a['title']} ({a['tracks']} tracks)",
                description=f"{a['artist']} — album",
                thumbnail_url=a["cover_url"] or None,
                input_message_content=InputTextMessageContent(
                    f"https://tidal.com/album/{a['id']}",
                ),
            )
        )
    for t in search_data["tracks"]:
        mins, secs = divmod(t["duration"], 60)
        results.append(
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=t["title"],
                description=f"{t['artist']} — {t['album']} · {mins}:{secs:02d}",
                thumbnail_url=t["cover_url"] or None,
                input_message_content=InputTextMessageContent(
                    f"https://tidal.com/track/{t['id']}",
                ),
            )
        )

    if not results:
        results.append(
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="No results",
                description=f"Nothing found for '{query}'",
                input_message_content=InputTextMessageContent(
                    f"No Tidal results for: {query}"
                ),
            )
        )
    await update.inline_query.answer(results, cache_time=30)


async def _inline_share(update: Update, playing: list[dict]):
    """Send share links for now-playing tracks."""
    results = []
    for entry in playing:
        desc = f"{entry['artist']} — {entry['title']}"
        share_url = await _get_share_url(entry["songId"], desc)
        if share_url:
            results.append(
                InlineQueryResultArticle(
                    id=str(uuid4()),
                    title=entry["title"],
                    description=f"{entry['artist']} — {entry['album']}",
                    input_message_content=InputTextMessageContent(share_url),
                )
            )
    if not results:
        results.append(
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="Sharing unavailable",
                description="NAVIDROME_PUBLIC_URL not configured or share failed",
                input_message_content=InputTextMessageContent(
                    "Sharing is not available."
                ),
            )
        )
    await update.inline_query.answer(results, cache_time=5)


async def _inline_now_playing(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               playing: list[dict]):
    """Stream and send now-playing tracks as cached audio."""
    user_id = update.effective_user.id

    # Check if any tracks are already cached
    cached_results = []
    uncached = []
    for entry in playing:
        song_id = entry["songId"]
        if song_id in _file_id_cache:
            cached_results.append(
                InlineQueryResultCachedAudio(
                    id=str(uuid4()),
                    audio_file_id=_file_id_cache[song_id],
                )
            )
        elif song_id not in _upload_failed:
            uncached.append(entry)

    if cached_results:
        await update.inline_query.answer(cached_results, cache_time=5)
        return

    # Nothing cached — start uploads in background, show placeholder immediately
    for entry in uncached:
        asyncio.create_task(_ensure_cached(context.bot, user_id, entry))

    if uncached:
        entry = uncached[0]
        await update.inline_query.answer([
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=f"{entry['artist']} — {entry['title']}",
                description="Loading audio, try again in a few seconds...",
                input_message_content=InputTextMessageContent(
                    f"Now playing: {entry['artist']} — {entry['title']}"
                ),
            )
        ], cache_time=3)
    else:
        await update.inline_query.answer([
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="Failed to load tracks",
                description="Could not stream from Navidrome",
                input_message_content=InputTextMessageContent("Failed to load tracks."),
            )
        ], cache_time=5)


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in ALLOWED_USERS:
        return

    query = (update.inline_query.query or "").strip()
    query_lower = query.lower()
    share_mode = query_lower in ("s", "share")
    now_playing_mode = query_lower in ("np", "now")
    delete_mode = query_lower == "del" or query_lower.startswith("del ")

    if delete_mode:
        return await _inline_delete(update, query[3:].strip())

    if not query or (0 < len(query) <= 2 and not share_mode and not now_playing_mode):
        return await _inline_hint(update)

    if not share_mode and not now_playing_mode:
        return await _inline_search(update, query)

    # Now-playing or share mode — need Navidrome data
    playing = await _get_now_playing_cached()
    if playing is None:
        await update.inline_query.answer([
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="Navidrome unavailable",
                description="Could not fetch now playing",
                input_message_content=InputTextMessageContent(
                    "Navidrome unavailable."
                ),
            )
        ], cache_time=5)
        return

    if not playing:
        await update.inline_query.answer([
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="Nothing playing",
                description="No tracks currently playing on Navidrome",
                input_message_content=InputTextMessageContent("Nothing is playing right now."),
            )
        ], cache_time=5)
        return

    if share_mode:
        return await _inline_share(update, playing)

    return await _inline_now_playing(update, context, playing)
