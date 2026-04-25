import asyncio
import contextlib
import functools
import logging
import os
import re
import shutil
import time
import uuid

_RESTART_DELAY = 30  # seconds between automatic restarts after a crash

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import NetworkError, TelegramError
from telegram.request import HTTPXRequest

from config import TG_TOKEN, ALLOWED_USERS, MUSIC_DIR, NAVI_LOGIN, NAVI_PASS, NAVI_PUBLIC_URL
import metadata
import soulseek
import navidrome
import retagger
from inline import handle_inline_query, _DELETE_PREFIX
from library.files import _sanitize, _find_existing_track

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Words that force re-download (delete existing + download fresh)
_FORCE_RE = re.compile(r"\b(re|force|redownload)\b", re.IGNORECASE)
# Music platform URLs accepted as download triggers. metadata.resolve_link
# handles them via platform-direct paths (Tidal/Spotify/Apple HTML scrape,
# iTunes Lookup) with Odesli as the long-tail fallback.
_MUSIC_LINK_RE = re.compile(
    r"https?://(?:"
    r"(?:listen\.|www\.)?tidal\.com"
    r"|open\.spotify\.com|spotify\.link"
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

_album_share_cache: dict[str, str] = {}
_CACHE_MAX = 500
# Serialise album/track downloads — slskd queues per-peer, and stacking
# multiple albums in flight just makes everything wait longer.
_download_semaphore = asyncio.Semaphore(1)
# album/track IDs currently downloading or queued — drops duplicate pastes
_in_flight: set[str] = set()

# Pending mp3-fallback prompts. Keyed by a short callback id; entry holds the
# already-found lossy candidates so we don't re-search on Accept. TTL keeps a
# stale prompt from auto-downloading hours later if the user finally taps.
_LOSSY_PROMPT_TTL = 300  # seconds
_pending_lossy: dict[str, dict] = {}

# Re-tagger session state. One global slot — single-user bot. Cleared after
# /retag confirm or /retag stop, or when the dry-run TTL expires.
_RETAG_SESSION_TTL = 1200  # 20 minutes
_retag_session: dict | None = None
_retag_in_progress = False


def _purge_stale_lossy() -> None:
    now = time.monotonic()
    for cid in [k for k, v in _pending_lossy.items() if v["expire_at"] <= now]:
        _pending_lossy.pop(cid, None)


def _format_lossy_summary(candidates: list) -> str:
    """One-liner like 'mp3 320 from 5 peers' for the prompt UI."""
    mp3 = [c for c in candidates if c.extension == "mp3"]
    m4a = [c for c in candidates if c.extension == "m4a"]
    parts = []
    if mp3:
        best_kbps = max((c.bit_rate or 0) for c in mp3) // 1000
        parts.append(f"mp3 {best_kbps}kbps from {len(mp3)} peer{'s' if len(mp3) != 1 else ''}")
    if m4a:
        parts.append(f"m4a from {len(m4a)} peer{'s' if len(m4a) != 1 else ''}")
    return " · ".join(parts) if parts else f"{len(candidates)} lossy peers"


def _short(e: BaseException, limit: int = 120) -> str:
    """Render an exception for user-facing chat messages.

    Strips full URLs — aiohttp / requests embed the failed URL in their
    str(exception), which can leak proxy hosts, internal Navidrome URLs, or
    auth tokens that snuck into a query string.
    """
    s = re.sub(r"https?://\S+", "<url>", str(e))
    return s if len(s) <= limit else s[: limit - 3] + "..."


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
        f"Send a music link (Tidal, Spotify, Apple Music, Deezer, etc.) to download.\n"
        f"Type <code>@{bot_me.username}</code> in any chat for inline mode.",
        parse_mode="HTML",
    )


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_me = await context.bot.get_me()
    await update.message.reply_text(
        "<b>Download</b>\n"
        "Send a music link from Tidal, Spotify, Apple Music, Deezer, Shazam, etc.\n"
        "Add <b>re</b> after the link to force re-download.\n\n"
        "<b>Inline mode</b>\n"
        f"<code>@{bot_me.username} song name</code> — search Deezer\n"
        f"<code>@{bot_me.username} np</code> — now playing as audio\n"
        f"<code>@{bot_me.username} s</code> — share link for current track\n"
        f"<code>@{bot_me.username} l</code> — lyrics for current track\n"
        f"<code>@{bot_me.username} lib name</code> — search library\n"
        f"<code>@{bot_me.username} del name</code> — delete album\n\n"
        "<b>Commands</b>\n"
        "/scan — trigger library rescan\n"
        "/stats — library statistics\n"
        "/retag — refresh tags on every album from Deezer + Last.fm "
        "(dry-run, then <code>/retag confirm</code>)",
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


def _format_eta(seconds: float) -> str:
    if not seconds or seconds <= 0:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def _outcome_tag(plan) -> str:
    """One-token outcome label for a finished AlbumPlan."""
    if plan.error:
        return "skipped"
    if plan.is_lastfm_only:
        return "Last.fm only"
    if not plan.needs_apply:
        return "clean"
    return "Deezer"


def _retag_progress_text(
    phase: str, idx: int, total: int, plan, started_at: float,
    tally_changed: int, tally_lastfm: int, tally_skipped: int,
) -> str:
    """Three-line download-style progress for /retag. Used in both dry-run
    and apply phases (caller picks ``phase``)."""
    pct = (idx * 100 // total) if total else 0
    elapsed = max(time.monotonic() - started_at, 0.001)
    avg = elapsed / max(idx, 1)
    eta = avg * max(total - idx, 0)
    head = f"{plan.artist_dir}/{plan.album_dir}" if plan else ""
    if len(head) > 60:
        head = head[:58] + "…"
    last_line = f"{head} · {_outcome_tag(plan)}" if plan else ""
    return (
        f"<b>{phase}</b> [{idx}/{total} · {pct}%]\n"
        f"{last_line}\n"
        f"changed {tally_changed} · last.fm {tally_lastfm} · "
        f"skipped {tally_skipped} · ETA {_format_eta(eta)}"
    )


def _format_retag_summary(plans: list, summary, elapsed: float, sample_n: int = 8) -> str:
    """Compact dry-run summary, ``Done!``-style. No emoji, label · value
    pairs separated by ·, sample lines indented two spaces.
    """
    will_change = [p for p in plans if p.needs_apply]
    sample = will_change[:sample_n]
    sample_lines = []
    for p in sample:
        head = f"{p.artist_dir} / {p.album_dir}"
        if len(head) > 70:
            head = head[:68] + "…"
        tag = ", ".join(c.split(":")[0] for c in p.changes[:4])
        if len(p.changes) > 4:
            tag += "…"
        sample_lines.append(f"  {head} — {tag}")

    failed = [p for p in plans if p.error]
    failed_lines = []
    for p in failed[:5]:
        head = f"{p.artist_dir} / {p.album_dir}"
        if len(head) > 70:
            head = head[:68] + "…"
        failed_lines.append(f"  {head}")

    out = [
        f"<b>Dry-run done</b> · {summary.total} albums in {_format_eta(elapsed)}",
        f"identified {summary.by_comment_id + summary.by_search} "
        f"({summary.by_comment_id} from tag, {summary.by_search} via search) · "
        f"unidentified {summary.unidentified}",
        f"would change {summary.will_change} · already canonical {summary.no_changes}",
    ]
    if sample_lines:
        out.append("")
        out.append(f"<b>Top changes</b> ({len(sample_lines)} of {summary.will_change}):")
        out.extend(sample_lines)
        if summary.will_change > sample_n:
            out.append(f"  … +{summary.will_change - sample_n} more")
    if failed_lines:
        out.append("")
        out.append(f"<b>Skipped</b> ({len(failed_lines)} of {len(failed)}):")
        out.extend(failed_lines)
        if len(failed) > 5:
            out.append(f"  … +{len(failed) - 5} more")
    if summary.will_change:
        out.append("")
        out.append(
            "Reply <code>/retag confirm</code> to apply, "
            "<code>/retag stop</code> to drop."
        )
    return "\n".join(out)


@authorized
async def cmd_retag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _retag_session, _retag_in_progress

    args = (context.args or [])
    sub = args[0].lower() if args else ""

    if sub == "stop":
        if _retag_session is None:
            await update.message.reply_text("No re-tag session pending.")
        else:
            _retag_session = None
            await update.message.reply_text("Re-tag session dropped.")
        return

    if sub == "confirm":
        if _retag_session is None:
            await update.message.reply_text(
                "No re-tag session to confirm. Run /retag first."
            )
            return
        if time.monotonic() > _retag_session["expire_at"]:
            _retag_session = None
            await update.message.reply_text(
                "Re-tag session expired (20 min). Run /retag again."
            )
            return
        if _retag_in_progress:
            await update.message.reply_text("Re-tag already running.")
            return
        plans = _retag_session["plans"]
        await _do_retag_apply(update, plans)
        return

    if _retag_in_progress:
        await update.message.reply_text("Re-tag already running.")
        return

    status_msg = await update.message.reply_text("Scanning library…")
    last_edit = [time.monotonic()]
    started = time.monotonic()
    tally = {"changed": 0, "lastfm": 0, "skipped": 0}

    async def progress(idx, total, plan):
        if plan.is_lastfm_only:
            tally["lastfm"] += 1
        elif plan.error:
            tally["skipped"] += 1
        elif plan.needs_apply:
            tally["changed"] += 1
        # Throttle edits — Telegram rate-limits ~1/sec on the same message.
        if time.monotonic() - last_edit[0] < 4 and idx != total:
            return
        last_edit[0] = time.monotonic()
        text = _retag_progress_text(
            "Dry-run", idx, total, plan, started,
            tally["changed"], tally["lastfm"], tally["skipped"],
        )
        try:
            await status_msg.edit_text(text, parse_mode="HTML")
        except TelegramError:
            pass

    _retag_in_progress = True
    try:
        try:
            plans, summary = await retagger.run_dry_run(MUSIC_DIR, progress=progress)
        except Exception as e:
            logger.exception("Re-tag dry-run failed")
            await status_msg.edit_text(f"Re-tag failed: {_short(e)}")
            return
    finally:
        _retag_in_progress = False

    elapsed = time.monotonic() - started
    _retag_session = {
        "plans": plans,
        "summary": summary,
        "expire_at": time.monotonic() + _RETAG_SESSION_TTL,
    }
    text = _format_retag_summary(plans, summary, elapsed)
    try:
        await status_msg.edit_text(text, parse_mode="HTML")
    except TelegramError:
        await update.message.reply_text(text, parse_mode="HTML")


async def _do_retag_apply(update: Update, plans: list):
    global _retag_session, _retag_in_progress
    _retag_in_progress = True
    status_msg = await update.message.reply_text("Applying re-tag…")
    last_edit = [time.monotonic()]
    last_album = [""]

    async def progress(idx, total, plan):
        last_album[0] = f"{plan.artist_dir}/{plan.album_dir}"
        if time.monotonic() - last_edit[0] < 5 and idx != total:
            return
        last_edit[0] = time.monotonic()
        try:
            await status_msg.edit_text(
                f"Applying [{idx}/{total}]\n  {last_album[0][:60]}"
            )
        except TelegramError:
            pass

    try:
        try:
            stats = await retagger.run_apply(plans, progress=progress)
        except Exception as e:
            logger.exception("Re-tag apply failed")
            await status_msg.edit_text(f"Apply failed: {_short(e)}")
            return
    finally:
        _retag_in_progress = False
        _retag_session = None

    out = [
        f"<b>Re-tag done</b>",
        f"  ✎ {stats['albums_planned']} albums touched",
        f"  ✍ {stats['files_written']} files written",
    ]
    if stats["files_skipped"]:
        out.append(f"  · {stats['files_skipped']} files skipped (no track match)")
    if stats["failed"]:
        out.append(f"  ✗ {len(stats['failed'])} album-level failures")
        for path, err in stats["failed"][:3]:
            out.append(f"      {path}: {_short(Exception(err))}")
    out.append("")
    out.append(await _trigger_scan())
    try:
        await status_msg.edit_text("\n".join(out), parse_mode="HTML")
    except TelegramError:
        await update.message.reply_text("\n".join(out), parse_mode="HTML")


@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""

    if text.startswith(_DELETE_PREFIX):
        rel_path = text[len(_DELETE_PREFIX):].strip()
        if rel_path:
            await _handle_delete(update, rel_path)
        return

    force = bool(_FORCE_RE.search(text))

    music_matches = _MUSIC_LINK_RE.findall(text)
    if music_matches:
        for url in music_matches:
            await _resolve_and_download(update, url, force=force)


async def _handle_delete(update: Update, rel_path: str):
    """Delete a local album folder and trigger rescan."""
    full_path = os.path.normpath(os.path.join(MUSIC_DIR, rel_path))
    # Resolve symlinks so a planted link like /music/X -> /etc can't escape the check.
    real_path = os.path.realpath(full_path)
    real_music_dir = os.path.realpath(MUSIC_DIR)

    if not real_path.startswith(real_music_dir + os.sep):
        await update.message.reply_text("Invalid path.")
        return
    depth = os.path.relpath(real_path, real_music_dir).count(os.sep)
    if depth < 1:
        await update.message.reply_text("Cannot delete top-level directories.")
        return

    if os.path.islink(full_path) or not os.path.isdir(full_path):
        await update.message.reply_text(f"Not found: {rel_path}")
        return

    artist_dir = os.path.dirname(full_path)
    album_name = os.path.basename(full_path)
    artist_name = os.path.basename(artist_dir)

    await asyncio.to_thread(shutil.rmtree, full_path)
    logger.info("Deleted album: %s/%s", artist_name, album_name)

    def _remove_if_empty(d: str) -> bool:
        try:
            if not os.listdir(d):
                os.rmdir(d)
                return True
        except OSError:
            pass
        return False

    if await asyncio.to_thread(_remove_if_empty, artist_dir):
        logger.info("Removed empty artist folder: %s", artist_name)

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


async def _resolve_and_download(update: Update, url: str, force: bool = False):
    status_msg = await update.message.reply_text("Resolving link...")
    try:
        result = await metadata.resolve_link(url)
    except Exception as e:
        logger.error("Odesli resolve failed for %s: %s", url, e)
        await status_msg.edit_text(f"Failed to resolve link: {_short(e)}")
        return

    if result is None:
        await status_msg.edit_text("Could not resolve link.")
        return

    link_type, album_or_track_id = result
    await status_msg.delete()

    if link_type == "album":
        await _download_album(update, album_or_track_id, force=force)
    else:
        await _download_track(update, album_or_track_id, force=force)


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


async def _download_album(update: Update, album_id: str, force: bool = False):
    key = f"album:{album_id}"
    if key in _in_flight:
        await update.message.reply_text("Already queued.")
        return
    _in_flight.add(key)
    try:
        await _do_download_album(update, album_id, force=force)
    finally:
        _in_flight.discard(key)


async def _do_download_album(update: Update, album_id: str, force: bool = False):
    if _download_semaphore.locked():
        status_msg = await update.message.reply_text("Queued, waiting for current download...")
    else:
        status_msg = await update.message.reply_text("Fetching album info...")

    async with _download_semaphore:
        try:
            album = await metadata.fetch_album(album_id)
            await metadata.enrich_genres(album)
        except Exception as e:
            logger.error("Failed to fetch album %s: %s", album_id, e)
            await status_msg.edit_text(f"Failed to fetch album info: {_short(e)}")
            return

        # Force re-download: rename existing album directory to .bak
        backup_dir = None
        if force:
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
        _last_edit = [0.0]

        async def progress(current, total, download_current, download_total, track_title,
                            transfer=None):
            # Throttle mid-album edits so Telegram doesn't rate-limit; always
            # emit the first and last so the user sees start and completion.
            now = time.monotonic()
            is_track_edge = current in (1, total) and transfer is None
            if not is_track_edge and (now - _last_edit[0]) < 3.0:
                return
            _last_edit[0] = now
            done = download_current - 1
            elapsed = now - _progress_start
            # Track-level live info from slskd: download speed + ETA for this file.
            track_extras = ""
            if transfer:
                speed_bps = transfer.get("speed_bps", 0) or 0
                eta_sec = transfer.get("eta_sec", 0) or 0
                pct = transfer.get("pct", 0) or 0
                if speed_bps > 0:
                    if speed_bps >= 1_048_576:
                        speed_s = f"{speed_bps / 1_048_576:.1f} MB/s"
                    else:
                        speed_s = f"{speed_bps / 1024:.0f} KB/s"
                    track_extras = f" · {pct:.0f}% · {speed_s}"
                    if eta_sec:
                        if eta_sec >= 60:
                            track_extras += f" · ~{eta_sec // 60}m {eta_sec % 60}s"
                        else:
                            track_extras += f" · ~{eta_sec}s"
            # Album-level ETA based on completed tracks
            album_eta = ""
            if done > 0 and not transfer:
                eta_sec = int((elapsed / done) * (download_total - done))
                if eta_sec >= 60:
                    album_eta = f" · ~{eta_sec // 60}m {eta_sec % 60}s left"
                else:
                    album_eta = f" · ~{eta_sec}s left"
            try:
                text = (
                    f"Downloading: {album['artist']} — {album['title']}\n"
                    f"[{current}/{total}] {track_title}"
                    f"{track_extras}{album_eta}"
                )
                await status_msg.edit_text(text)
            except TelegramError:
                pass

        try:
            result = await soulseek.download_album(
                album_id, MUSIC_DIR, progress=progress, album=album,
            )
        except Exception as e:
            logger.error("Album download failed (%s — %s): %s", album["artist"], album["title"], e)
            if backup_dir and os.path.isdir(backup_dir):
                target = backup_dir.removesuffix(".bak")
                if os.path.exists(target):
                    shutil.rmtree(target)
                os.rename(backup_dir, target)
                logger.info("Force re-download failed, restored backup: %s", target)
            await status_msg.edit_text(f"Download failed: {_short(e)}")
            return

        if backup_dir and os.path.isdir(backup_dir):
            shutil.rmtree(backup_dir)
            logger.info("Force re-download succeeded, removed backup: %s", backup_dir)

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

        if result["downloaded"] > 0:
            done_text += "\n" + await _trigger_scan()

        # Share link works even for "already in library" — useful when re-pasting old albums.
        share_url = await _try_share_album(album["artist"], album["title"], skip_delay=result["downloaded"] == 0)
        if share_url:
            done_text += f"\n\n{share_url}"

        await _send_result(update, status_msg, done_text, result["album_dir"])


async def _download_track(update: Update, track_id: str, force: bool = False):
    key = f"track:{track_id}"
    if key in _in_flight:
        await update.message.reply_text("Already queued.")
        return
    _in_flight.add(key)
    try:
        await _do_download_track(update, track_id, force=force)
    finally:
        _in_flight.discard(key)


async def _do_download_track(update: Update, track_id: str, force: bool = False):
    if _download_semaphore.locked():
        status_msg = await update.message.reply_text("Queued, waiting for current download...")
    else:
        status_msg = await update.message.reply_text("Fetching track info...")

    async with _download_semaphore:
        try:
            track, album_ctx = await metadata.fetch_single_track(track_id)
            if album_ctx:
                await metadata.enrich_genres(album_ctx)
        except Exception as e:
            logger.error("Failed to fetch track %s: %s", track_id, e)
            await status_msg.edit_text(f"Failed to fetch track info: {_short(e)}")
            return

        if force:
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

        try:
            path, was_downloaded, fmt = await soulseek.download_single_track(
                track, album_ctx, MUSIC_DIR,
            )
        except RuntimeError as e:
            if "No FLAC found" in str(e):
                if await _offer_mp3_fallback(update, status_msg, track_id, track, album_ctx):
                    return
                await status_msg.edit_text(
                    f"No FLAC or mp3 fallback found for {track['artist']} — {track['title']}"
                )
                return
            logger.error("Track download failed (%s — %s): %s", track["artist"], track["title"], e)
            await status_msg.edit_text(f"Download failed: {_short(e)}")
            return
        except Exception as e:
            logger.error("Track download failed (%s — %s): %s", track["artist"], track["title"], e)
            await status_msg.edit_text(f"Download failed: {_short(e)}")
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


async def _offer_mp3_fallback(
    update: Update, status_msg, track_id: str, track: dict, album_ctx: dict,
) -> bool:
    """Probe Soulseek for mp3/m4a candidates after a FLAC search came up empty.
    If any are available, send an inline-keyboard prompt and stash the
    candidates for the callback handler. Returns True when a prompt was sent
    (caller leaves status_msg alone), False when there's nothing to fall back
    to (caller writes the final failure message).
    """
    _purge_stale_lossy()
    try:
        candidates = await soulseek.find_lossy_candidates(track, album_ctx)
    except Exception as e:
        logger.warning("Lossy fallback search failed: %s", e)
        return False
    if not candidates:
        return False

    cid = uuid.uuid4().hex[:12]
    _pending_lossy[cid] = {
        "track_id": track_id,
        "track": track,
        "album_ctx": album_ctx,
        "candidates": candidates,
        "chat_id": status_msg.chat_id,
        "message_id": status_msg.message_id,
        "expire_at": time.monotonic() + _LOSSY_PROMPT_TTL,
    }

    summary = _format_lossy_summary(candidates)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✓ Accept {summary}", callback_data=f"lossy:accept:{cid}")],
        [InlineKeyboardButton("✗ Skip", callback_data=f"lossy:skip:{cid}")],
    ])
    try:
        await status_msg.edit_text(
            f"FLAC not found for {track['artist']} — {track['title']}\n\n"
            f"Lossy fallback available: {summary}",
            reply_markup=keyboard,
        )
    except TelegramError as e:
        logger.warning("Failed to send lossy-fallback prompt: %s", e)
        _pending_lossy.pop(cid, None)
        return False
    return True


async def _handle_lossy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    user_id = query.from_user.id if query.from_user else None
    if user_id not in ALLOWED_USERS:
        await query.answer("Not allowed.", show_alert=False)
        return
    parts = query.data.split(":", 2)
    if len(parts) != 3 or parts[0] != "lossy":
        return
    action, cid = parts[1], parts[2]

    _purge_stale_lossy()
    entry = _pending_lossy.pop(cid, None)
    if entry is None:
        await query.answer("Prompt expired.", show_alert=False)
        with contextlib.suppress(TelegramError):
            await query.edit_message_text("Prompt expired (5 min).")
        return

    await query.answer()

    track = entry["track"]
    album_ctx = entry["album_ctx"]
    candidates = entry["candidates"]

    if action == "skip":
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(
                f"Skipped — FLAC not available for {track['artist']} — {track['title']}"
            )
        return

    if action != "accept":
        return

    summary = _format_lossy_summary(candidates)
    with contextlib.suppress(TelegramError):
        await query.edit_message_text(
            f"Downloading: {track['artist']} — {track['title']} ({summary})"
        )

    in_flight_key = f"track:{entry['track_id']}:lossy"
    if in_flight_key in _in_flight:
        with contextlib.suppress(TelegramError):
            await query.edit_message_text("Already queued.")
        return
    _in_flight.add(in_flight_key)
    try:
        async with _download_semaphore:
            try:
                path, _was, fmt = await soulseek.download_single_track(
                    track, album_ctx, MUSIC_DIR,
                    accept_lossy=True,
                    precomputed_candidates=candidates,
                )
            except Exception as e:
                logger.error(
                    "Lossy download failed (%s — %s): %s",
                    track["artist"], track["title"], e,
                )
                with contextlib.suppress(TelegramError):
                    await query.edit_message_text(f"Lossy download failed: {_short(e)}")
                return

            scan_note = await _trigger_scan()
            fmt_str = f" · {fmt}" if fmt else ""
            done_text = (
                f"Done! {track['artist']} — {track['title']}{fmt_str}\n{scan_note}"
            )
            share_url = await _try_share_album(
                album_ctx.get("artist", track["artist"]),
                album_ctx.get("title", ""),
                skip_delay=False,
            )
            if share_url:
                done_text += f"\n\n{share_url}"
            with contextlib.suppress(TelegramError):
                await query.edit_message_text(done_text)
    finally:
        _in_flight.discard(in_flight_key)


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


async def _reset_bot_pools(app: Application) -> None:
    """Drop pooled HTTPX connections so a dead TCP tunnel isn't reused after the link recovers."""
    for req in app.bot._request:
        try:
            await req.shutdown()
            await req.initialize()
        except Exception:
            pass


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, NetworkError):
        logger.warning("Network error (will retry): %s", context.error)
        await _reset_bot_pools(context.application)
        return
    logger.exception("Unhandled exception", exc_info=context.error)


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("help", "Show all features"),
        BotCommand("scan", "Trigger library rescan"),
        BotCommand("stats", "Library statistics"),
        BotCommand("retag", "Re-tag library from current Deezer + Last.fm metadata"),
    ])


async def _shutdown(app: Application) -> None:
    await metadata.close()
    await soulseek.close()
    await navidrome.close()


def _build_app() -> Application:
    app = (
        Application.builder()
        .token(TG_TOKEN)
        .get_updates_request(HTTPXRequest(
            connect_timeout=10.0,
            read_timeout=20.0,
            write_timeout=10.0,
            pool_timeout=5.0,
        ))
        .post_init(_post_init)
        .post_shutdown(_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("retag", cmd_retag))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(InlineQueryHandler(handle_inline_query))
    app.add_handler(CallbackQueryHandler(_handle_lossy_callback, pattern=r"^lossy:"))
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
