"""Album browsing and scoped library actions."""

import asyncio
import logging
import os
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from flows import common, retag, library_actions
from library.files import list_audio_files
import config
import retagger

logger = logging.getLogger(__name__)


@common.authorized
async def cmd_library(update: Update, context: ContextTypes.DEFAULT_TYPE):
    term = " ".join(context.args or []).casefold()
    albums = await asyncio.to_thread(retagger.scan_albums, config.MUSIC_DIR)
    matches = [(path, artist, album) for path, artist, album in albums
               if term in f"{artist} {album}".casefold()][:20]
    choices = {}
    buttons = []
    for path, artist, album in matches:
        if not library_actions._safe_library_album(path):
            continue
        token = uuid.uuid4().hex[:12]
        choices[token] = (path, update.effective_chat.id)
        buttons.append([InlineKeyboardButton(f"{artist} — {album}"[:100],
                                            callback_data=f"album:open:{token}")])
    context.user_data["library_albums"] = choices
    await update.message.reply_text(
        "Select an album. Use /library <name> to narrow the search (up to 20 results)."
        if buttons else "No albums found.",
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None)


@common.authorized
async def _handle_album_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _prefix, action, token = query.data.split(":", 2)
    await query.answer()
    choice = context.user_data.get("library_albums", {}).get(token)
    if not choice or choice[1] != update.effective_chat.id or not library_actions._safe_library_album(choice[0]):
        await query.message.reply_text("Album selection expired. Use /library again.")
        return
    folder = choice[0]
    if action == "preview":
        await retag._retag_command(update, [], folder=folder)
    elif action == "maintain":
        await retag._show_maintenance_picker(update, context, folder)
    elif action == "open":
        files = await asyncio.to_thread(list_audio_files, folder)
        title = f"{os.path.basename(os.path.dirname(folder))} — {os.path.basename(folder)}"
        await query.message.reply_text(
            title + f"\n{len(files)} tracks\n\n" + "\n".join(os.path.relpath(f, folder) for f in files)[:3000],
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Preview tag changes", callback_data=f"album:preview:{token}")],
                [InlineKeyboardButton("Update selected fields", callback_data=f"album:maintain:{token}")],
            ]))
