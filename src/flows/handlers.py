"""Telegram input parsing, commands, and flow dispatch."""

import asyncio
import contextlib
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from flows import common, library_actions, retag, downloads, catalog
from inline import _DELETE_PREFIX
import config
import providers

logger = logging.getLogger(__name__)


_FORCE_RE = re.compile(r"\b(re|force|redownload)\b", re.IGNORECASE)


_MUSIC_LINK_RE = re.compile(
    r"https?://(?:"
    r"(?:listen\.|www\.)?tidal\.com"
    r"|open\.spotify\.com|spotify\.link"
    r"|music\.apple\.com"
    r"|(?:www\.)?deezer\.com"
    r"|(?:music\.|www\.|m\.)?youtube\.com|youtu\.be"
    r"|(?:www\.)?song\.link|(?:www\.)?odesli\.co|(?:www\.)?album\.link"
    r"|soundcloud\.com"
    r"|music\.amazon\.com"
    r"|(?:www\.)?shazam\.com"
    r")/\S+",
    re.IGNORECASE,
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    if user_id not in config.ALLOWED_USERS:
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
    msg = await update.message.reply_text(
        f"<b>Commands:</b>\n"
        f"/help — show all features\n"
        f"/scan — trigger Navidrome library rescan\n"
        f"/upload — import your music\n"
        f"/maintain — choose library fields to update\n"
        f"/provider — download provider status and setup\n\n"
        f"Send a music link to identify a release and check your library.\n"
        f"Import album ZIPs or audio files with /upload.\n"
        f"Type <code>@{bot_me.username}</code> in any chat for inline mode, "
        f"or use the buttons below.",
        parse_mode="HTML",
        reply_markup=_chat_shortcuts(update),
    )
    # Pinned, the shortcut buttons stay one tap away at the top of the chat.
    # Only once: every pin also posts a "pinned" service line.
    with contextlib.suppress(TelegramError):
        pinned = (await context.bot.get_chat(msg.chat_id)).pinned_message
        if not (pinned and pinned.from_user and pinned.from_user.id == context.bot.id):
            await msg.pin(disable_notification=True)


def _inline_shortcuts() -> InlineKeyboardMarkup:
    """Buttons that put ``@bot <mode>`` into the input field, so inline modes
    don't have to be typed. The last one opens a chat picker instead."""
    b = InlineKeyboardButton
    return InlineKeyboardMarkup([
        [b("🔎 Search", switch_inline_query_current_chat=""),
         b("📚 Library", switch_inline_query_current_chat="lib "),
         b("🗑 Delete", switch_inline_query_current_chat="del ")],
        [b("▶️ Playing", switch_inline_query_current_chat="np"),
         b("📝 Lyrics", switch_inline_query_current_chat="l"),
         b("🔗 Share", switch_inline_query_current_chat="s")],
        [b("🎵 Send library audio", switch_inline_query_current_chat="audio ")],
        [b("📤 Send playing to…", switch_inline_query="np")],
    ])


def _chat_shortcuts(update: Update) -> InlineKeyboardMarkup:
    markup = _inline_shortcuts()
    rows = [[common._import_button()]]
    chat = getattr(update, "effective_chat", None)
    if config.ALLOWED_USERS and chat and chat.id == config.ALLOWED_USERS[0]:
        rows.append([InlineKeyboardButton("📦 Pending uploads", callback_data="uploads:pending")])
    return InlineKeyboardMarkup([*rows, *markup.inline_keyboard])


@common.authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_me = await context.bot.get_me()
    await update.message.reply_text(
        "<b>Import and identify</b>\n"
        "Use /upload to import an album ZIP or audio files.\n"
        "Send a music link from Tidal, Spotify, Apple Music, Deezer, Shazam or YouTube. "
        "For other services, search by name. The bot checks your library first.\n"
        "Downloads require a configured, available provider. "
        "Add <b>re</b> after the link to force re-download.\n"
        "Every download and upload import carries a ✖ Cancel button until it "
        "finishes — tap it at any stage; tracks already saved stay.\n\n"
        "<b>Inline mode</b>\n"
        f"<code>@{bot_me.username} song name</code> — search Deezer\n"
        f"<code>@{bot_me.username} np</code> — now playing as audio\n"
        f"<code>@{bot_me.username} s</code> — share link for current track\n"
        f"<code>@{bot_me.username} l</code> — lyrics for current track\n"
        f"<code>@{bot_me.username} lib name</code> — search library links\n"
        f"<code>@{bot_me.username} audio name</code> — select and send library audio\n"
        f"<code>@{bot_me.username} del name</code> — delete an album or a track\n\n"
        "<b>Commands</b>\n"
        "/scan — trigger Navidrome library rescan\n"
        "/upload — import your music\n"
        "/provider — download provider status and setup\n"
        "/stats — library statistics\n"
        "/library name — open an album and preview its tag changes\n"
        "/maintain — choose library fields to update\n"
        "/import ID release — manually select an upload release\n"
        "/retag — refresh tags on every album from Deezer + Last.fm "
        "(dry-run, then <code>/retag confirm</code>)",
        parse_mode="HTML",
        reply_markup=_chat_shortcuts(update),
    )


@common.authorized
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = await library_actions._trigger_scan()
    await update.message.reply_text(note)


@common.authorized
async def cmd_share_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = await library_actions._trigger_scan(slskd_mode="immediate")
    await update.message.reply_text(note)


@common.authorized
async def cmd_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await providers.check()
    await update.message.reply_text(status.message, reply_markup=common._provider_actions())


@common.authorized
async def _handle_import_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await common._send_import_help(update.callback_query.message)


@common.authorized
async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await common._send_import_help(update.message)


@common.authorized
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await asyncio.to_thread(library_actions._collect_stats)
    size_gb = stats["bytes"] / (1024 ** 3)
    await update.message.reply_text(
        f"Artists: {stats['artists']}\n"
        f"Albums: {stats['albums']}\n"
        f"Tracks: {stats['tracks']}\n"
        f"Size: {size_gb:.1f} GB",
    )


@common.authorized
async def cmd_maintain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await retag._show_maintenance_picker(update, context)


@common.authorized
async def cmd_retag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await retag._retag_command(update, context.args or [])


@common.authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""

    if text.startswith(_DELETE_PREFIX):
        rel_path = text[len(_DELETE_PREFIX):].strip()
        if rel_path:
            await library_actions._handle_delete(update, rel_path)
        return

    # Match force words against the text with URLs removed — album/track slugs
    # like ".../album/re-animator" contain hyphen-delimited "re" that would
    # otherwise silently trigger a destructive re-download.
    force = bool(_FORCE_RE.search(_MUSIC_LINK_RE.sub(" ", text)))

    music_matches = _MUSIC_LINK_RE.findall(text)
    if music_matches:
        for url in music_matches:
            await downloads._resolve_and_download(update, url, force=force)


async def _handle_lossy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await downloads._handle_lossy_callback(update, context)


async def _handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await common._handle_cancel_callback(update, context)


async def _handle_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await library_actions._handle_delete_callback(update, context)


async def _handle_album_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await catalog._handle_album_callback(update, context)


async def _handle_maintenance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await retag._handle_maintenance_callback(update, context)


async def _handle_retag_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await retag._handle_retag_callback(update, context)


async def cmd_library(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await catalog.cmd_library(update, context)
