import asyncio
import functools
import logging
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
from telegram.ext import (
    Application,
    CommandHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import NetworkError
from telegram.request import HTTPXRequest

from config import TG_TOKEN, ALLOWED_USERS, MUSIC_DIR
import monochrome
import navidrome

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

ALBUM_RE = re.compile(r"(?:monochrome\.samidy\.com|monochrome\.tf|tidal\.com)/album/(\d+)")
TRACK_RE = re.compile(r"(?:monochrome\.samidy\.com|monochrome\.tf|tidal\.com)/track/(\d+)")

# song_id -> telegram file_id
_file_id_cache: dict[str, str] = {}
# song_id -> asyncio.Event (signals upload completion)
_upload_events: dict[str, asyncio.Event] = {}
# entry_id -> share URL
_share_url_cache: dict[str, str] = {}
# serialize album/track downloads so Tidal CDN doesn't throttle
_download_semaphore = asyncio.Semaphore(1)


def authorized(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in ALLOWED_USERS:
            return
        return await func(update, context)
    return wrapper


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text(
            "This is a private bot for managing a Navidrome music library.\n\n"
            f"Your user ID: <code>{user_id}</code>\n\n"
            "If you're the server admin, add this ID to <b>ALLOWED_USERS</b> "
            "in your bot configuration.\n\n"
            '<a href="https://github.com/eeegoloauq/music-bot">GitHub</a>',
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info("Unauthorized /start from user %s", user_id)
        return
    bot_me = await context.bot.get_me()
    await update.message.reply_text(
        f"<b>Commands:</b>\n"
        f"/help — show all features\n"
        f"/scan — trigger library rescan\n\n"
        f"Send a Tidal/Monochrome link to download.\n"
        f"Type <code>@{bot_me.username}</code> in any chat to share now playing.",
        parse_mode="HTML",
    )


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_me = await context.bot.get_me()
    await update.message.reply_text(
        "<b>Download</b>\n"
        "Send a tidal.com or monochrome album/track link.\n\n"
        "<b>Inline mode</b>\n"
        f"<code>@{bot_me.username}</code> — send now playing as audio\n"
        f"<code>@{bot_me.username} share</code> — send share link with cover art\n\n"
        "<b>Commands</b>\n"
        "/scan — trigger library rescan",
        parse_mode="HTML",
    )


@authorized
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await navidrome.start_scan()
        await update.message.reply_text("Library scan started.")
    except Exception as e:
        logger.exception("Scan failed")
        await update.message.reply_text(f"Scan failed: {e}")


@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""

    album_match = ALBUM_RE.search(text)
    track_match = TRACK_RE.search(text) if not album_match else None

    if album_match:
        await _download_album(update, album_match.group(1))
    elif track_match:
        await _download_track(update, track_match.group(1))


async def _download_album(update: Update, album_id: str):
    if _download_semaphore.locked():
        status_msg = await update.message.reply_text("Queued, waiting for current download...")
    else:
        status_msg = await update.message.reply_text("Fetching album info...")

    async with _download_semaphore:
        try:
            album = await monochrome.fetch_album(album_id)
            await status_msg.edit_text(
                f"Downloading: {album['artist']} — {album['title']}\n"
                f"Tracks: {len(album['tracks'])}"
            )

            async def progress(current, total, track_title):
                try:
                    await status_msg.edit_text(
                        f"Downloading: {album['artist']} — {album['title']}\n"
                        f"Track {current}/{total}: {track_title}"
                    )
                except Exception:
                    pass

            result = await monochrome.download_album(album_id, MUSIC_DIR, progress=progress, album=album)

            if result["downloaded"] == 0 and not result["failed"]:
                done_text = f"Already in library: {album['artist']} — {album['title']}"
            else:
                parts = []
                if result["downloaded"]:
                    parts.append(f"{result['downloaded']} saved")
                if result["skipped"]:
                    parts.append(f"{result['skipped']} skipped")
                if result["failed"]:
                    parts.append(f"{len(result['failed'])} failed")
                done_text = f"Done! {album['artist']} — {album['title']}\n" + ", ".join(parts) + "."

            if result["downloaded"] > 0:
                await navidrome.start_scan()
                done_text += "\nLibrary scan triggered."

            await status_msg.edit_text(done_text)

            share_url = await _try_share_album(album["artist"], album["title"])
            if share_url:
                await status_msg.edit_text(f"{done_text}\n\n{share_url}")
        except Exception as e:
            logger.exception("Album download failed")
            await status_msg.edit_text(f"Download failed: {e}")


async def _download_track(update: Update, track_id: str):
    if _download_semaphore.locked():
        status_msg = await update.message.reply_text("Queued, waiting for current download...")
    else:
        status_msg = await update.message.reply_text("Fetching track info...")

    async with _download_semaphore:
        try:
            track, album_ctx = await monochrome.fetch_single_track(track_id)
            await status_msg.edit_text(
                f"Downloading: {track['artist']} — {track['title']}"
            )

            path, was_downloaded = await monochrome.download_single_track(track, album_ctx, MUSIC_DIR)

            if was_downloaded:
                await navidrome.start_scan()
                done_text = (
                    f"Done! {track['artist']} — {track['title']}\n"
                    "Library scan triggered."
                )
            else:
                done_text = f"Already in library: {track['artist']} — {track['title']}"

            await status_msg.edit_text(done_text)

            share_url = await _try_share_album(
                album_ctx.get("artist", track["artist"]),
                album_ctx.get("title", ""),
            )
            if share_url:
                await status_msg.edit_text(f"{done_text}\n\n{share_url}")
        except Exception as e:
            logger.exception("Track download failed")
            await status_msg.edit_text(f"Download failed: {e}")


async def _try_share_album(artist: str, title: str) -> str | None:
    """Wait for Navidrome to index, then create a share link for the album."""
    try:
        await asyncio.sleep(3)
        album_id = await navidrome.search_album(artist, title)
        if not album_id:
            return None
        if album_id in _share_url_cache:
            return _share_url_cache[album_id]
        url = await navidrome.create_share(album_id, f"{artist} — {title}")
        if url:
            _share_url_cache[album_id] = url
        return url
    except Exception:
        logger.exception("Failed to create share link")
        return None


async def _get_share_url(entry_id: str, description: str) -> str | None:
    """Get or create a share URL for an entry, with caching."""
    if entry_id in _share_url_cache:
        return _share_url_cache[entry_id]
    try:
        url = await navidrome.create_share(entry_id, description)
        if url:
            _share_url_cache[entry_id] = url
        return url
    except Exception:
        logger.exception("Failed to create share link")
        return None


async def _ensure_cached(bot, user_id: int, entry: dict) -> str | None:
    """Make sure a song is uploaded to Telegram. Returns file_id or None."""
    song_id = entry["songId"]

    if song_id in _file_id_cache:
        logger.info("Inline: %s — %s (cached)", entry["artist"], entry["title"])
        return _file_id_cache[song_id]

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
        cover_coro = navidrome.get_cover_art(entry["coverArtId"]) if entry.get("coverArtId") else asyncio.sleep(0)
        audio_result, cover_result = await asyncio.gather(
            navidrome.stream_song(song_id), cover_coro,
        )
        audio_data, filename = audio_result
        t1 = time.monotonic()
        size_mb = len(audio_data) / (1024 * 1024)
        dl_speed = size_mb / (t1 - t0) if (t1 - t0) > 0 else 0
        logger.info("Inline: %s — %s | stream %.1fMB in %.1fs (%.1f MB/s)",
                     entry["artist"], entry["title"], size_mb, t1 - t0, dl_speed)

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
            logger.error("Upload resulted in non-Audio type for %s", song_id)
            await msg.delete()
            return None
        await msg.delete()
        t2 = time.monotonic()
        up_speed = size_mb / (t2 - t1) if (t2 - t1) > 0 else 0
        logger.info("Inline: %s — %s | upload %.1fs (%.1f MB/s) | total %.1fs",
                     entry["artist"], entry["title"], t2 - t1, up_speed, t2 - t0)
        return _file_id_cache[song_id]
    except Exception:
        logger.exception("Upload failed for %s", song_id)
        return None
    finally:
        event.set()
        _upload_events.pop(song_id, None)


async def handle_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in ALLOWED_USERS:
        return

    query = (update.inline_query.query or "").strip().lower()
    share_mode = "share" in query

    try:
        playing = await navidrome.get_now_playing()
    except Exception:
        logger.exception("Failed to get now playing")
        playing = []

    if not playing:
        await update.inline_query.answer(
            [InlineQueryResultArticle(
                id=str(uuid4()),
                title="Nothing playing",
                description="No tracks currently playing on Navidrome",
                input_message_content=InputTextMessageContent("Nothing is playing right now."),
            )],
            cache_time=5,
        )
        return

    if share_mode:
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
        return

    # Audio mode (default)
    results = []
    for entry in playing:
        file_id = await _ensure_cached(context.bot, user_id, entry)
        if file_id:
            results.append(
                InlineQueryResultCachedAudio(
                    id=str(uuid4()),
                    audio_file_id=file_id,
                )
            )

    if not results:
        results.append(
            InlineQueryResultArticle(
                id=str(uuid4()),
                title="Failed to load tracks",
                description="Could not download from Navidrome",
                input_message_content=InputTextMessageContent("Failed to load tracks."),
            )
        )

    await update.inline_query.answer(results, cache_time=5)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, NetworkError):
        logger.warning("Network error (will retry): %s", context.error)
        return
    logger.exception("Unhandled exception", exc_info=context.error)


async def _shutdown(app: Application) -> None:
    await monochrome.close()
    await navidrome.close()


def main():
    app = (
        Application.builder()
        .token(TG_TOKEN)
        .get_updates_request(HTTPXRequest(pool_timeout=5.0))
        .post_shutdown(_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(InlineQueryHandler(handle_inline_query))
    app.add_error_handler(_error_handler)

    logger.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
