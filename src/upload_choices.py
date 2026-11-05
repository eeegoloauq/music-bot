"""Owner-only choices shown on upload, at restart and through chat buttons."""

import asyncio
import logging
import os
import re

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup
from telegram.error import TelegramError

from config import ALLOWED_USERS, UPLOAD_DIR
import metadata
import uploads

logger = logging.getLogger(__name__)


class UploadChoices:
    def __init__(self, start_import, io_factory):
        self.start_import = start_import
        self.io_factory = io_factory
        self.busy: set[str] = set()

    def _owner(self, update):
        return (ALLOWED_USERS and update.effective_user
                and update.effective_user.id == ALLOWED_USERS[0]
                and update.effective_chat.id == ALLOWED_USERS[0])

    async def offer(self, io, report):
        if report.error:
            await io.reply_text(uploads.format_rejection(report))
            return
        key = os.path.basename(report.staging_dir)
        await io.reply_text(
            f"📦 {report.name}: {len(report.audio)} audio file(s).\n"
            "Keep original tags, artwork and filenames, or identify a release.\n"
            f"To select a release manually: /import {key} <album link or Deezer ID>\n"
            "Files stay here until you choose. Pending uploads are available from the start menu.",
            reply_markup=InlineKeyboardMarkup([
                [Button("Keep original files", callback_data=f"import:original:{key}")],
                [Button("Identify release", callback_data=f"import:match:{key}")],
            ]))

    async def list_pending(self, update, context):
        if not self._owner(update):
            return
        await update.callback_query.answer()
        io = self.io_factory(context.bot, update.effective_chat.id)
        if not await self.restore(io):
            await io.reply_text("No uploads waiting for import.")

    async def restore(self, io):
        """Show the same choices for unfinished uploads without starting imports."""
        reports = await asyncio.to_thread(uploads.staged_reports, UPLOAD_DIR)
        reports = [r for r in reports if os.path.basename(r.staging_dir) not in self.busy]
        for report in reports:
            try:
                await self.offer(io, report)
            except TelegramError:
                logger.warning("Could not show pending upload %s", report.name, exc_info=True)
        return bool(reports)

    async def _find(self, key):
        if not re.fullmatch(r"[a-f0-9]{12}", key):
            return None
        reports = await asyncio.to_thread(uploads.staged_reports, UPLOAD_DIR)
        return next((r for r in reports if os.path.basename(r.staging_dir) == key), None)

    def _start(self, context, update, report, mode, album_id=None):
        key = os.path.basename(report.staging_dir)
        self.busy.add(key)

        async def run():
            try:
                io = self.io_factory(context.bot, update.effective_chat.id)
                await self.start_import(io, report, mode=mode, album_id=album_id)
            finally:
                self.busy.discard(key)

        context.application.create_task(run(), update=update)

    async def callback(self, update, context):
        if not self._owner(update):
            return
        query = update.callback_query
        _prefix, mode, key = query.data.split(":", 2)
        if mode not in {"original", "match"}:
            return
        await query.answer()
        report = await self._find(key)
        if not report or key in self.busy:
            await query.message.reply_text("Upload unavailable or already importing.")
            return
        # No await between reserving this upload and starting its task.
        self._start(context, update, report, mode)

    async def manual(self, update, context):
        if not self._owner(update):
            return
        args = context.args or []
        if len(args) != 2:
            await update.message.reply_text("Usage: /import <upload ID> <album link or Deezer ID>. Use the upload message above.")
            return
        key, release = args
        report = await self._find(key)
        if not report or key in self.busy:
            await update.message.reply_text("Upload unavailable or already importing.")
            return
        # Reserve before the resolver yields; a second button cannot import it.
        self.busy.add(key)
        scheduled = False
        try:
            match = ("album", release) if release.isdecimal() else await metadata.resolve_link(release)
            if not match or match[0] != "album":
                await update.message.reply_text("Select an album link or a Deezer album ID.")
                return
            self._start(context, update, report, "match", str(match[1]))
            scheduled = True
        except Exception:
            await update.message.reply_text("Release lookup failed. Files remain in staging; try again.")
        finally:
            # The task owns the reservation once scheduled.
            if not scheduled:
                self.busy.discard(key)
