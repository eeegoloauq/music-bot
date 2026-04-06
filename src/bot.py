import asyncio
import functools
import logging
import os
import re
import shutil
import time

_RESTART_DELAY = 30  # seconds between automatic restarts after a crash

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import NetworkError, TelegramError
from telegram.request import HTTPXRequest

from config import TG_TOKEN, ALLOWED_USERS, MUSIC_DIR, NAVI_LOGIN, NAVI_PASS, NAVI_PUBLIC_URL
import tidal
import navidrome
from inline import handle_inline_query, _DELETE_PREFIX

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

ALBUM_RE = re.compile(r"(?:monochrome\.samidy\.com|monochrome\.tf|tidal\.com)/album/(\d+)")
TRACK_RE = re.compile(r"(?:monochrome\.samidy\.com|monochrome\.tf|tidal\.com)/track/(\d+)")
# Words after the link that trigger HI_RES_LOSSLESS for this download
_HIRES_RE = re.compile(r"\b(hi|hq|hires|hi-res)\b", re.IGNORECASE)
# Words that force re-download (delete existing + download fresh)
_FORCE_RE = re.compile(r"\b(re|force|redownload)\b", re.IGNORECASE)
# Known music platform URLs that Odesli can resolve to Tidal
_MUSIC_LINK_RE = re.compile(
    r"https?://(?:"
    r"open\.spotify\.com|spotify\.link"
    r"|music\.apple\.com"
    r"|(?:www\.)?deezer\.com"
    r"|music\.youtube\.com"
    r"|(?:www\.)?song\.link|(?:www\.)?odesli\.co|(?:www\.)?album\.link"
    r"|soundcloud\.com"
    r"|music\.amazon\.com"
    r"|(?:www\.)?shazam\.com"
    r")/\S+",
    re.IGNORECASE,
)

# album share URL cache for download handler (bounded)
_album_share_cache: dict[str, str] = {}
_CACHE_MAX = 500
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
    # Deep-link from inline help button: /start help
    if context.args and context.args[0] == "help":
        return await cmd_help(update, context)
    bot_me = await context.bot.get_me()
    await update.message.reply_text(
        f"<b>Commands:</b>\n"
        f"/help — show all features\n"
        f"/scan — trigger library rescan\n\n"
        f"Send a Tidal link or any music link (Spotify, Apple Music, etc.) to download.\n"
        f"Type <code>@{bot_me.username}</code> in any chat for inline mode.",
        parse_mode="HTML",
    )


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_me = await context.bot.get_me()
    await update.message.reply_text(
        "<b>Download</b>\n"
        "Send a Tidal, Spotify, Apple Music, Deezer, Shazam or other music link.\n"
        "Add <b>hi</b> after link for hi-res, <b>re</b> to force re-download.\n\n"
        "<b>Inline mode</b>\n"
        f"<code>@{bot_me.username} song name</code> — search Tidal\n"
        f"<code>@{bot_me.username} np</code> — now playing as audio\n"
        f"<code>@{bot_me.username} s</code> — share link for current track\n"
        f"<code>@{bot_me.username} l</code> — lyrics for current track\n"
        f"<code>@{bot_me.username} lib name</code> — search library\n"
        f"<code>@{bot_me.username} del name</code> — delete album\n\n"
        "<b>Commands</b>\n"
        "/scan — trigger library rescan\n"
        "/stats — library statistics",
        parse_mode="HTML",
    )


@authorized
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = await _trigger_scan()
    await update.message.reply_text(note)


def _collect_stats() -> dict:
    """Walk MUSIC_DIR and collect library statistics."""
    artists = 0
    albums = 0
    tracks = 0
    total_bytes = 0
    for artist_entry in os.scandir(MUSIC_DIR):
        if not artist_entry.is_dir() or artist_entry.name.startswith((".", "lost")):
            continue
        artists += 1
        for album_entry in os.scandir(artist_entry.path):
            if not album_entry.is_dir():
                continue
            albums += 1
            for f in os.scandir(album_entry.path):
                if f.is_file():
                    ext = os.path.splitext(f.name)[1].lower()
                    if ext in (".flac", ".mp3", ".m4a", ".ogg", ".opus"):
                        tracks += 1
                    try:
                        total_bytes += f.stat().st_size
                    except OSError:
                        pass
    return {"artists": artists, "albums": albums, "tracks": tracks, "bytes": total_bytes}


@authorized
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await asyncio.to_thread(_collect_stats)
    size_gb = stats["bytes"] / (1024 ** 3)
    await update.message.reply_text(
        f"Artists: {stats['artists']}\n"
        f"Albums: {stats['albums']}\n"
        f"Tracks: {stats['tracks']}\n"
        f"Size: {size_gb:.1f} GB",
    )


@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""

    if text.startswith(_DELETE_PREFIX):
        rel_path = text[len(_DELETE_PREFIX):].strip()
        if rel_path:
            await _handle_delete(update, rel_path)
        return

    quality = "HI_RES_LOSSLESS" if _HIRES_RE.search(text) else None
    force = bool(_FORCE_RE.search(text))

    album_matches = ALBUM_RE.findall(text)
    track_matches = TRACK_RE.findall(text) if not album_matches else []
    music_matches = _MUSIC_LINK_RE.findall(text) if not album_matches and not track_matches else []

    if album_matches:
        for album_id in album_matches:
            await _download_album(update, album_id, quality=quality, force=force)
    elif track_matches:
        for track_id in track_matches:
            await _download_track(update, track_id, quality=quality, force=force)
    elif music_matches:
        for url in music_matches:
            await _resolve_and_download(update, url, text, force=force)


async def _handle_delete(update: Update, rel_path: str):
    """Delete a local album folder and trigger rescan."""
    full_path = os.path.normpath(os.path.join(MUSIC_DIR, rel_path))

    # Safety: must be inside MUSIC_DIR and at least 2 levels deep (Artist/Album)
    if not full_path.startswith(os.path.normpath(MUSIC_DIR) + os.sep):
        await update.message.reply_text("Invalid path.")
        return
    depth = os.path.relpath(full_path, MUSIC_DIR).count(os.sep)
    if depth < 1:
        await update.message.reply_text("Cannot delete top-level directories.")
        return

    if not os.path.isdir(full_path):
        await update.message.reply_text(f"Not found: {rel_path}")
        return

    artist_dir = os.path.dirname(full_path)
    album_name = os.path.basename(full_path)
    artist_name = os.path.basename(artist_dir)

    shutil.rmtree(full_path)
    logger.info("Deleted album: %s/%s", artist_name, album_name)

    # Remove empty artist folder
    try:
        if not os.listdir(artist_dir):
            os.rmdir(artist_dir)
            logger.info("Removed empty artist folder: %s", artist_name)
    except OSError:
        pass

    scan_note = await _trigger_scan()
    await update.message.reply_text(f"Deleted: {artist_name} — {album_name}\n{scan_note}")


async def _trigger_scan() -> str:
    """Attempt a Navidrome library scan. Returns a status line for the user."""
    if not NAVI_LOGIN or not NAVI_PASS:
        return "Library scan not configured (NAVIDROME_USER/NAVIDROME_PASS not set)."
    try:
        await navidrome.start_scan()
        return "Library scan triggered."
    except navidrome.NavidromeAuthError:
        logger.warning("Navidrome scan: wrong credentials")
        return "Library scan failed: wrong credentials."
    except Exception as e:
        logger.warning("Navidrome scan failed: %s", e)
        return "Library scan failed."


async def _resolve_and_download(update: Update, url: str, suffix: str, force: bool = False):
    """Resolve a non-Tidal music link via Odesli, then download."""
    status_msg = await update.message.reply_text("Resolving link...")
    try:
        result = await tidal.resolve_link(url)
    except Exception as e:
        logger.error("Odesli resolve failed for %s: %s", url, e)
        await status_msg.edit_text(f"Failed to resolve link: {e}")
        return

    if result is None:
        await status_msg.edit_text("Not found on Tidal.")
        return

    link_type, tidal_id = result
    quality = "HI_RES_LOSSLESS" if _HIRES_RE.search(suffix) else None
    await status_msg.delete()

    if link_type == "album":
        await _download_album(update, tidal_id, quality=quality, force=force)
    else:
        await _download_track(update, tidal_id, quality=quality, force=force)


async def _send_result(update: Update, status_msg, text: str, album_dir: str) -> None:
    """Send download result with cover art if available, otherwise plain text."""
    cover_path = os.path.join(album_dir, "cover.jpg")
    if os.path.isfile(cover_path):
        try:
            with open(cover_path, "rb") as cover_f:
                await update.message.reply_photo(photo=cover_f, caption=text)
            await status_msg.delete()
            return
        except (TelegramError, OSError):
            pass
    try:
        await status_msg.edit_text(text)
    except TelegramError:
        pass


async def _download_album(update: Update, album_id: str, quality: str | None = None, force: bool = False):
    if _download_semaphore.locked():
        status_msg = await update.message.reply_text("Queued, waiting for current download...")
    else:
        status_msg = await update.message.reply_text("Fetching album info...")

    async with _download_semaphore:
        # Step 1: fetch metadata
        try:
            album = await tidal.fetch_album(album_id)
        except Exception as e:
            logger.error("Failed to fetch album %s: %s", album_id, e)
            await status_msg.edit_text(f"Failed to fetch album info: {e}")
            return

        # Force re-download: rename existing album directory to .bak
        backup_dir = None
        if force:
            from tidal.files import _sanitize
            album_dir = os.path.join(MUSIC_DIR, _sanitize(album["artist"]), _sanitize(album["title"]))
            if os.path.isdir(album_dir):
                backup_dir = album_dir + ".bak"
                if os.path.exists(backup_dir):
                    shutil.rmtree(backup_dir)
                os.rename(album_dir, backup_dir)
                logger.info("Force re-download: renamed %s -> .bak", album_dir)

        try:
            await status_msg.edit_text(
                f"Downloading: {album['artist']} — {album['title']}\n"
                f"Tracks: {len(album['tracks'])}"
            )
        except TelegramError:
            pass

        _progress_start = time.monotonic()

        async def progress(current, total, download_current, download_total, track_title):
            done = download_current - 1  # downloads finished before this one
            elapsed = time.monotonic() - _progress_start
            if done > 0:
                eta_sec = (elapsed / done) * (download_total - done)
                if eta_sec >= 60:
                    eta = f"~{int(eta_sec // 60)}m {int(eta_sec % 60)}s left"
                else:
                    eta = f"~{int(eta_sec)}s left"
            else:
                eta = ""
            try:
                text = (
                    f"Downloading: {album['artist']} — {album['title']}\n"
                    f"[{current}/{total}] {track_title}"
                )
                if eta:
                    text += f" · {eta}"
                await status_msg.edit_text(text)
            except TelegramError:
                pass

        # Step 2: download
        try:
            result = await tidal.download_album(
            album_id, MUSIC_DIR, progress=progress, album=album, quality=quality
        )
        except Exception as e:
            logger.error("Album download failed (%s — %s): %s", album["artist"], album["title"], e)
            # Restore backup on failure
            if backup_dir and os.path.isdir(backup_dir):
                target = backup_dir.removesuffix(".bak")
                if os.path.exists(target):
                    shutil.rmtree(target)
                os.rename(backup_dir, target)
                logger.info("Force re-download failed, restored backup: %s", target)
            await status_msg.edit_text(f"Download failed: {e}")
            return

        # Clean up backup after successful download
        if backup_dir and os.path.isdir(backup_dir):
            shutil.rmtree(backup_dir)
            logger.info("Force re-download succeeded, removed backup: %s", backup_dir)

        # Build result text
        if result["downloaded"] == 0 and not result["failed"]:
            done_text = f"Already in library: {album['artist']} — {album['title']}"
        else:
            parts = []
            if result["downloaded"]:
                saved = f"{result['downloaded']} saved"
                if result.get("format"):
                    saved += f" · {result['format']}"
                if result.get("with_lyrics"):
                    saved += f" · lyrics {result['with_lyrics']}/{result['downloaded']}"
                parts.append(saved)
            if result["skipped"]:
                parts.append(f"{result['skipped']} skipped")
            if result["failed"]:
                shown = result["failed"][:3]
                details = ", ".join(f"{t} ({r})" for t, r in shown)
                if len(result["failed"]) > 3:
                    details += f" +{len(result['failed']) - 3} more"
                parts.append(f"{len(result['failed'])} failed: {details}")
            done_text = f"Done! {album['artist']} — {album['title']}\n" + ", ".join(parts) + "."

        # Step 3: scan (non-critical)
        if result["downloaded"] > 0:
            done_text += "\n" + await _trigger_scan()

        # Step 4: share link (non-critical) — always try, even for "already in library"
        share_url = await _try_share_album(album["artist"], album["title"], skip_delay=result["downloaded"] == 0)
        if share_url:
            done_text += f"\n\n{share_url}"

        await _send_result(update, status_msg, done_text, result["album_dir"])


async def _download_track(update: Update, track_id: str, quality: str | None = None, force: bool = False):
    if _download_semaphore.locked():
        status_msg = await update.message.reply_text("Queued, waiting for current download...")
    else:
        status_msg = await update.message.reply_text("Fetching track info...")

    async with _download_semaphore:
        # Step 1: fetch metadata
        try:
            track, album_ctx = await tidal.fetch_single_track(track_id)
        except Exception as e:
            logger.error("Failed to fetch track %s: %s", track_id, e)
            await status_msg.edit_text(f"Failed to fetch track info: {e}")
            return

        # Force re-download: remove existing track file
        if force:
            from tidal.files import _sanitize, _find_existing_track
            album_dir = os.path.join(
                MUSIC_DIR, _sanitize(album_ctx.get("artist", track["artist"])),
                _sanitize(album_ctx.get("title", "Singles")),
            )
            existing = _find_existing_track(album_dir, track) if os.path.isdir(album_dir) else None
            if existing:
                os.remove(existing)
                logger.info("Force re-download: removed %s", existing)

        try:
            await status_msg.edit_text(f"Downloading: {track['artist']} — {track['title']}")
        except TelegramError:
            pass

        # Step 2: download
        try:
            path, was_downloaded, fmt = await tidal.download_single_track(
            track, album_ctx, MUSIC_DIR, quality=quality
        )
        except Exception as e:
            logger.error("Track download failed (%s — %s): %s", track["artist"], track["title"], e)
            await status_msg.edit_text(f"Download failed: {e}")
            return

        if was_downloaded:
            scan_note = await _trigger_scan()
            fmt_str = f" · {fmt}" if fmt else ""
            done_text = f"Done! {track['artist']} — {track['title']}{fmt_str}\n{scan_note}"
        else:
            done_text = f"Already in library: {track['artist']} — {track['title']}"

        share_url = await _try_share_album(
            album_ctx.get("artist", track["artist"]),
            album_ctx.get("title", ""),
            skip_delay=not was_downloaded,
        )
        if share_url:
            done_text += f"\n\n{share_url}"

        await _send_result(update, status_msg, done_text, os.path.dirname(path))


async def _try_share_album(artist: str, title: str, skip_delay: bool = False) -> str | None:
    """Wait for Navidrome to index, then create a share link for the album."""
    if not NAVI_PUBLIC_URL:
        return None
    try:
        if not skip_delay:
            await asyncio.sleep(3)
        album_id = await navidrome.search_album(artist, title)
        if not album_id:
            return None
        if album_id in _album_share_cache:
            return _album_share_cache[album_id]
        url = await navidrome.create_share(album_id, f"{artist} — {title}")
        if url:
            _album_share_cache[album_id] = url
            if len(_album_share_cache) > _CACHE_MAX:
                oldest = next(iter(_album_share_cache))
                del _album_share_cache[oldest]
        return url
    except navidrome.NavidromeAuthError:
        logger.warning("Navidrome share: wrong credentials")
        return None
    except Exception as e:
        logger.warning("Failed to create share link: %s", e)
        return None


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, NetworkError):
        logger.warning("Network error (will retry): %s", context.error)
        return
    logger.exception("Unhandled exception", exc_info=context.error)


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("help", "Show all features"),
        BotCommand("scan", "Trigger library rescan"),
        BotCommand("stats", "Library statistics"),
    ])


async def _shutdown(app: Application) -> None:
    await tidal.close()
    await navidrome.close()


def _build_app() -> Application:
    app = (
        Application.builder()
        .token(TG_TOKEN)
        .get_updates_request(HTTPXRequest(pool_timeout=5.0))
        .post_init(_post_init)
        .post_shutdown(_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(InlineQueryHandler(handle_inline_query))
    app.add_error_handler(_error_handler)
    return app


def main():
    if not NAVI_LOGIN or not NAVI_PASS:
        logger.warning(
            "NAVIDROME_USER/NAVIDROME_PASS not set — scan, inline audio, and share will not work"
        )
    if not NAVI_PUBLIC_URL:
        logger.info("NAVIDROME_PUBLIC_URL not set — share links disabled")

    logger.info("Bot starting...")
    while True:
        try:
            _build_app().run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)
            break  # clean shutdown (SIGTERM / SIGINT)
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception as e:
            logger.error("Bot crashed: %s — restarting in %ds", e, _RESTART_DELAY)
            time.sleep(_RESTART_DELAY)
            logger.info("Bot restarting...")


if __name__ == "__main__":
    main()
