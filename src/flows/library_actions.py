"""Library statistics, guarded deletion, scans, and share links."""

import asyncio
import contextlib
import logging
import os
import shutil
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from flows import state
from library.files import AUDIO_EXTS, _title_from_filename
import config
import navidrome
import providers

logger = logging.getLogger(__name__)


_album_share_cache: dict[str, str] = {}


_CACHE_MAX = 500


_pending_deletes: dict[str, tuple[str, bool]] = {}


def _collect_stats() -> dict:
    """Walk MUSIC_DIR and collect library statistics."""
    artists = 0
    albums = 0
    tracks = 0
    total_bytes = 0
    for artist_entry in os.scandir(config.MUSIC_DIR):
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
                    if ext in AUDIO_EXTS:
                        tracks += 1
                    try:
                        total_bytes += f.stat().st_size
                    except OSError:
                        pass
    return {"artists": artists, "albums": albums, "tracks": tracks, "bytes": total_bytes}


def _remove_if_empty(d: str) -> bool:
    try:
        if not os.listdir(d):
            os.rmdir(d)
            return True
    except OSError:
        pass
    return False


# Known sidecars may leave with the final track; unknown files keep the folder.
_ALBUM_LEFTOVER_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".lrc", ".txt", ".nfo", ".cue", ".log",
    ".m3u", ".m3u8", ".json", ".db", ".ini", ".url",
})


def _only_leftovers(d: str) -> bool:
    return all(os.path.splitext(n)[1].lower() in _ALBUM_LEFTOVER_EXTS
               for _, _, files in os.walk(d) for n in files)


def _count_audio(d: str) -> int:
    return sum(os.path.splitext(n)[1].lower() in AUDIO_EXTS
               for _, _, files in os.walk(d) for n in files)


def _check_delete_path(rel_path: str) -> tuple[str, str]:
    """``(full path, "")`` for an album folder or an audio track inside
    MUSIC_DIR, else ``("", reason for the user)``."""
    full_path = os.path.normpath(os.path.join(config.MUSIC_DIR, rel_path))
    # Resolve symlinks so a planted link like /music/X -> /etc can't escape the check.
    real_path = os.path.realpath(full_path)
    real_music_dir = os.path.realpath(config.MUSIC_DIR)

    if not real_path.startswith(real_music_dir + os.sep):
        return "", "Invalid path."
    if os.path.relpath(real_path, real_music_dir).count(os.sep) < 1:
        return "", "Cannot delete top-level directories."
    is_track = (os.path.isfile(full_path)
                and os.path.splitext(full_path)[1].lower() in AUDIO_EXTS)
    if os.path.islink(full_path) or not (os.path.isdir(full_path) or is_track):
        return "", f"Not found: {rel_path}"
    return full_path, ""


def _delete_label(full_path: str) -> str:
    """"Artist — Album" for a folder, "Artist — Album — Title" for a track."""
    parts = os.path.relpath(full_path, config.MUSIC_DIR).split(os.sep)
    if os.path.isfile(full_path):
        parts[-1] = _title_from_filename(parts[-1])
    return " — ".join(parts)


async def _handle_delete(update: Update, rel_path: str):
    """Ask before deleting an album folder or a single track: one tap in the
    inline list sends the ``delete:`` message, and a mistap must not cost files."""
    full_path, error = _check_delete_path(rel_path)
    if error:
        await update.message.reply_text(error)
        return
    is_dir = os.path.isdir(full_path)
    question = f"Delete {_delete_label(full_path)}"
    if is_dir:
        question += f" ({await asyncio.to_thread(_count_audio, full_path)} tracks)"
    cid = uuid.uuid4().hex[:12]
    _pending_deletes[cid] = (rel_path, is_dir)
    await update.message.reply_text(f"{question}?", reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Delete", callback_data=f"rm:yes:{cid}"),
        InlineKeyboardButton("✖ Cancel", callback_data=f"rm:no:{cid}"),
    ]]))


async def _handle_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    user_id = query.from_user.id if query.from_user else None
    if user_id not in config.ALLOWED_USERS:
        await query.answer("Not allowed.", show_alert=False)
        return
    _, action, cid = query.data.split(":", 2)
    # Popped, so a second tap on the same prompt finds nothing to delete.
    pending = _pending_deletes.pop(cid, None)
    if pending is None:
        await query.answer("Prompt expired.", show_alert=False)
        with contextlib.suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)
        return
    with contextlib.suppress(TelegramError):
        await query.answer()
    rel_path, was_dir = pending
    if action != "yes":
        text = "Cancelled."
    else:
        # Checked again: the library may have changed since the question,
        # and a track prompt must never delete a folder.
        full_path, text = _check_delete_path(rel_path)
        if full_path and os.path.isdir(full_path) != was_dir:
            text = f"Changed since the question, not deleted: {rel_path}"
        elif full_path:
            text = await (_delete_album if was_dir else _delete_track)(full_path)
    with contextlib.suppress(TelegramError):
        await query.edit_message_text(text)


async def _delete_album(full_path: str) -> str:
    label = _delete_label(full_path)
    artist_dir = os.path.dirname(full_path)
    await asyncio.to_thread(shutil.rmtree, full_path)
    logger.info("Deleted album: %s", os.path.relpath(full_path, config.MUSIC_DIR))
    state._invalidate_retag_session("album deleted")
    if await asyncio.to_thread(_remove_if_empty, artist_dir):
        logger.info("Removed empty artist folder: %s", os.path.basename(artist_dir))
    scan_note = await _trigger_scan(slskd_mode="scheduled")
    return f"Deleted: {label}\n{scan_note}"


async def _delete_track(full_path: str) -> str:
    """Delete one track. When only covers, lyrics and the like are left in its
    album folder, the folder and an emptied artist folder go too — only for
    the usual Artist/Album/track layout, so it never climbs to the library root."""
    label = _delete_label(full_path)
    album_dir = os.path.dirname(full_path)
    tidy = (os.path.relpath(full_path, config.MUSIC_DIR).count(os.sep) == 2
            and not os.path.islink(album_dir))  # rmtree refuses links

    def _remove() -> bool:
        os.remove(full_path)
        # Walked, not listed: files in a nested CD1/ folder count too.
        if not tidy or not _only_leftovers(album_dir):
            return False
        shutil.rmtree(album_dir)
        return True

    album_gone = await asyncio.to_thread(_remove)
    logger.info("Deleted track: %s", os.path.relpath(full_path, config.MUSIC_DIR))
    state._invalidate_retag_session("track deleted")
    if album_gone:
        logger.info("Removed album folder with no audio left: %s",
                    os.path.relpath(album_dir, config.MUSIC_DIR))
        artist_dir = os.path.dirname(album_dir)
        if await asyncio.to_thread(_remove_if_empty, artist_dir):
            logger.info("Removed empty artist folder: %s", os.path.basename(artist_dir))
    scan_note = await _trigger_scan(slskd_mode="scheduled")
    return f"Deleted: {label}\n{scan_note}"


async def _trigger_scan(*, slskd_mode: str = "none") -> str:
    """Trigger Navidrome library scan + slskd share rescan scheduling.

    Navidrome reflects new tags to playback users, so it runs immediately.
    slskd exposes only a full share scan and it is expensive; normal downloads
    skip it, delete/retag schedule a delayed quiet-period scan, and explicit
    /sharescan requests an immediate slskd scan.
    Returns a status line for the user.
    """
    if slskd_mode != "none" and not providers.sharing_enabled():
        slskd_note = "Soulseek share scans are disabled." if slskd_mode == "immediate" else ""
    elif slskd_mode == "immediate":
        asyncio.create_task(providers.rescan_shares())
        slskd_note = "slskd share scan triggered."
    elif slskd_mode == "scheduled":
        delay = providers.schedule_rescan_shares()
        mins = max(1, round(delay / 60))
        slskd_note = f"slskd share scan scheduled in ~{mins}m."
    else:
        slskd_note = ""

    if not config.NAVI_LOGIN or not config.NAVI_PASS:
        note = "Library scan not configured (NAVIDROME_USER/NAVIDROME_PASS not set)."
        return f"{note} {slskd_note}".strip()
    try:
        await navidrome.start_scan()
        note = "Library scan triggered."
        return f"{note} {slskd_note}".strip()
    except navidrome.NavidromeAuthError:
        logger.warning("Navidrome scan: wrong credentials")
        note = "Library scan failed: wrong credentials."
        return f"{note} {slskd_note}".strip()
    except Exception as e:
        logger.warning("Navidrome scan failed: %s", e)
        note = "Library scan failed."
        return f"{note} {slskd_note}".strip()


async def _try_share_album(artist: str, title: str, skip_delay: bool = False) -> str | None:
    """Wait for Navidrome to index, then create a share link for the album."""
    if not config.NAVI_PUBLIC_URL:
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


def _safe_library_album(path: str) -> bool:
    root = os.path.realpath(config.MUSIC_DIR)
    return (os.path.realpath(path).startswith(root + os.sep)
            and os.path.isdir(path)
            and not os.path.islink(path)
            and not os.path.islink(os.path.dirname(path)))
