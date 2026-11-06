"""Cancellable runs, Telegram status delivery, and backup recovery."""

import asyncio
import contextlib
import functools
import logging
import os
import re
import shutil
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import ContextTypes

from library.importing import run_to_completion
import config

logger = logging.getLogger(__name__)


_active_runs: dict[str, "ActiveRun"] = {}


class ActiveRun:
    """One cancellable run: owns the ✖ Cancel keyboard, the task the button
    cancels, and the bookkeeping the cancel report needs. Registered on
    creation; ``close()`` retires the button (later taps get "already
    finished")."""

    def __init__(self, chat_id: int | None, kind: str):
        self.id = uuid.uuid4().hex[:12]        # "cancel:" + 12 hex ≤ 64 bytes
        self.chat_id = chat_id
        self.kind = kind                        # album | track | upload | prompt
        self.task: asyncio.Task | None = None
        self.cancel_requested = False
        self.on_prompt_cancel = None            # prompt runs: async () -> None
        self.saved_fn = None                    # () -> tracks in the library
        self.total: int | None = None
        self.backup: tuple[str | None, str | None] | None = None
        self.note = ""
        _active_runs[self.id] = self

    def cancel_row(self) -> list[InlineKeyboardButton]:
        return [InlineKeyboardButton("✖ Cancel", callback_data=f"cancel:{self.id}")]

    @property
    def markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([self.cancel_row()])

    @property
    def open(self) -> bool:
        return self.id in _active_runs

    def attach(self) -> None:
        """Bind to the current task."""
        self.task = asyncio.current_task()

    def close(self) -> None:
        _active_runs.pop(self.id, None)

    def cancel_text(self) -> str:
        if self.note:
            return f"✖ Cancelled — {self.note}"
        saved = self.saved_fn() if self.saved_fn else 0
        if self.kind == "track":
            return ("✖ Cancelled — the track was already saved" if saved
                    else "✖ Cancelled — nothing was downloaded")
        if self.total is None:
            return "✖ Cancelled — nothing was downloaded yet"
        text = f"✖ Cancelled — {int(saved)}/{self.total} tracks were already saved"
        if self.kind == "upload":
            text += "; the rest stays in uploads staging"
        return text


class RunStatus:
    """A run's status message. Telegram drops the inline keyboard on any
    edit that doesn't resend it, so non-final edits re-attach the ✖ Cancel
    button while the run is open; ``safe_edit(final=True)`` passes
    ``reply_markup=None`` and the final text retires the button."""

    def __init__(self, msg, run: ActiveRun):
        self._msg = msg
        self.run = run
        self.message_id = msg.message_id
        self.chat_id = getattr(msg, "chat_id", None)

    async def edit_text(self, text: str, **kwargs):
        kwargs.setdefault("reply_markup", self.run.markup if self.run.open else None)
        return await self._msg.edit_text(text, **kwargs)

    async def delete(self, *args, **kwargs):
        return await self._msg.delete(*args, **kwargs)


class _QueryMessage:
    """The prompt message a callback query was tapped on, edited through
    the query — same edit/delete surface as a Message."""

    def __init__(self, query):
        self._q = query
        msg = query.message
        self.message_id = getattr(msg, "message_id", None)
        self.chat_id = getattr(msg, "chat_id", None)

    async def edit_text(self, text: str, **kwargs):
        return await self._q.edit_message_text(text, **kwargs)

    async def delete(self, *args, **kwargs):
        return await self._q.delete_message()


def _short(e: BaseException, limit: int = 120) -> str:
    """Render an exception for user-facing chat messages.

    Strips full URLs — aiohttp / requests embed the failed URL in their
    str(exception), which can leak proxy hosts, internal Navidrome URLs, or
    auth tokens that snuck into a query string.
    """
    s = re.sub(r"https?://\S+", "<url>", str(e))
    return s if len(s) <= limit else s[: limit - 3] + "..."


class LiveStatus:
    """One live-updating status message. Renders the current progress model
    and edits the message in place — throttled so phase changes land fast
    (≥1s apart) while high-frequency transfer updates coalesce (≥3s)."""

    PHASE_INTERVAL = 1.0
    PROGRESS_INTERVAL = 3.0

    def __init__(self, edit_fn, render_fn):
        self._edit = edit_fn        # async (html_text) -> None
        self._render = render_fn    # () -> html_text
        self._last_edit = 0.0
        self._last_text = ""

    async def refresh(self, force: bool = False):
        now = time.monotonic()
        interval = self.PHASE_INTERVAL if force else self.PROGRESS_INTERVAL
        if now - self._last_edit < interval:
            return
        text = self._render()
        if text == self._last_text:
            return
        self._last_edit = now
        self._last_text = text
        try:
            await self._edit(text)
        except TelegramError:
            pass


_EDIT_BACKOFF = (2, 5, 10)


_FINAL_BACKOFF = (2, 5, 10, 20, 30)


async def _tg_call(coro_fn, what: str, final: bool = False):
    """Run a Telegram send/edit, absorbing failures. Returns the call's
    result, or None once every attempt failed — never raises."""
    backoff = _FINAL_BACKOFF if final else _EDIT_BACKOFF
    honored_flood = False
    last_err: TelegramError | None = None
    for delay in (*backoff, None):
        try:
            return await coro_fn()
        except RetryAfter as e:
            # Honor Telegram's flood-control wait once; a second one means
            # we're being told to go away for real.
            last_err = e
            if honored_flood or delay is None:
                break
            honored_flood = True
            await asyncio.sleep(e.retry_after)
        except BadRequest as e:
            # Subclasses NetworkError but isn't transient — retrying won't
            # change the answer.
            last_err = e
            break
        except NetworkError as e:  # includes TimedOut
            last_err = e
            if delay is None:
                break
            await asyncio.sleep(delay)
        except TelegramError as e:
            last_err = e
            break
    logger.log(logging.ERROR if final else logging.WARNING,
               "%s dropped after Telegram errors: %s", what, last_err)
    return None


class _NullMessage:
    """Stand-in when even the initial status send failed: edits/deletes
    no-op, so the flow runs UI-less instead of dying over messaging."""
    message_id = None
    chat_id = None

    async def edit_text(self, *args, **kwargs):
        return None

    async def delete(self, *args, **kwargs):
        return None


async def safe_edit(status_msg, text: str, final: bool = False, **kwargs) -> bool:
    """Best-effort status edit; True when the edit landed."""
    if status_msg is None:
        return False
    if final:
        # The final text carries no ✖ Cancel button (RunStatus injects it
        # only when the key is absent).
        kwargs.setdefault("reply_markup", None)
    res = await _tg_call(lambda: status_msg.edit_text(text, **kwargs),
                         "status edit", final=final)
    return res is not None


async def safe_send(sender, text: str, reply_markup=None):
    """Best-effort initial status send. ``sender`` is anything with
    ``reply_text`` (ChatIO, telegram Message). On total failure returns a
    _NullMessage — the download/import is the job, the UI is best-effort."""
    extra = {"reply_markup": reply_markup} if reply_markup is not None else {}
    msg = await _tg_call(lambda: sender.reply_text(text, **extra), "status send")
    return msg if msg is not None else _NullMessage()


def authorized(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in config.ALLOWED_USERS:
            return
        return await func(update, context)
    return wrapper


_PROVIDER_GUIDE = "https://github.com/eeegoloauq/music-bot/tree/main/docs/providers"


_UPLOAD_GUIDE = "https://github.com/eeegoloauq/music-bot/blob/main/docs/setup.md#local-imports"


def _import_button() -> InlineKeyboardButton:
    if config.UPLOAD_WEB_URL:
        return InlineKeyboardButton("🌐 Upload page", url=config.UPLOAD_WEB_URL)
    return InlineKeyboardButton("📥 Import files", callback_data="import:help")


def _provider_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_import_button(), InlineKeyboardButton("Providers", url=_PROVIDER_GUIDE)],
    ])


async def _send_import_help(message):
    if config.UPLOAD_WEB_URL:
        text = "Upload an album ZIP or audio file. Choose how to import it here."
        button = _import_button()
    else:
        text = ("Copy an album ZIP or audio files to the server's upload folder. "
                "Import choices appear here.")
        button = InlineKeyboardButton("Upload setup", url=_UPLOAD_GUIDE)
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup([[button]]))


class ChatIO:
    """The chat a download reports into. Wraps the only Telegram sends the
    download flows need, so a journal resume — which has no ``Update`` —
    drives the exact same code path as a freshly pasted link."""

    def __init__(self, bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id

    @classmethod
    def from_update(cls, update: Update) -> "ChatIO":
        return cls(update.get_bot(), update.effective_chat.id)

    async def reply_text(self, text: str, reply_markup=None):
        extra = {"reply_markup": reply_markup} if reply_markup is not None else {}
        return await self.bot.send_message(self.chat_id, text, **extra)

    async def reply_photo(self, photo, caption: str):
        return await self.bot.send_photo(self.chat_id, photo=photo, caption=caption,
                                         parse_mode="HTML")


async def _send_result(io: ChatIO, status_msg, text: str, album_dir: str) -> None:
    """Send download result (HTML) with cover art if available, else plain text."""
    cover_path = os.path.join(album_dir, "cover.jpg")
    if os.path.isfile(cover_path):
        try:
            with open(cover_path, "rb") as cover_f:
                await io.reply_photo(cover_f, caption=text)
            await status_msg.delete()
            return
        except (TelegramError, OSError):
            pass
    await safe_edit(status_msg, text, final=True, parse_mode="HTML")


def _spawn_run(coro, io: ChatIO, what: str) -> None:
    """Start a download as its own task. The update loop processes one
    update at a time, so a download awaited inside a handler would block
    every later update — the ✖ Cancel tap included — until it finished."""
    async def go():
        try:
            if await coro is False:
                await io.reply_text("Already queued.")
        except Exception:
            logger.exception("%s task died", what)
    _spawn_background(go())


async def _await_in_own_task(coro):
    """Await ``coro`` as a separate task, so the ✖ Cancel aimed at it
    reaches only that run — not the resume driver or upload watcher
    awaiting it. A cancelled run returns normally (its cancel is a reported
    outcome), so the awaiting loop just moves on."""
    return await asyncio.create_task(coro)


async def _report_cancelled(status_msg, run: ActiveRun, io: ChatIO | None = None) -> None:
    """Final word on a user-cancelled run: a force backup goes back, the
    status message gets the honest tally, the button goes away."""
    if run.backup:
        backup, run.backup = run.backup, None
        run.note = ("the original copy was restored" if await _restore_backup(*backup)
                    else "restoring the original copy FAILED — check the library")
    text = run.cancel_text()
    logger.info("Cancelled by user: %s — %s", run.kind, text)
    if status_msg is not None:
        await safe_edit(status_msg, text, final=True)
    elif io is not None:
        await _tg_call(lambda: io.reply_text(text), "cancel notice", final=True)


async def _restore_backup(backup_dir: str | None, restore_target: str | None) -> bool:
    """Move a staged force-redownload backup (album dir or single file) back
    to its original location, discarding whatever the failed/partial fresh
    download left there. Returns True when the original is back in place;
    False when there was nothing to restore or the move failed."""
    if not (backup_dir and restore_target and os.path.exists(backup_dir)):
        return False
    try:
        if os.path.isdir(restore_target):
            await asyncio.to_thread(shutil.rmtree, restore_target)
        elif os.path.exists(restore_target):
            os.remove(restore_target)
        os.rename(backup_dir, restore_target)
        logger.info("Restored original: %s", restore_target)
        return True
    except OSError as e:
        logger.error("Failed to restore backup %s -> %s: %s", backup_dir, restore_target, e)
        return False


def _drop_backup(backup: str | None) -> None:
    if backup:
        with contextlib.suppress(OSError):
            os.remove(backup)


async def _put_back(run: "ActiveRun | None", backup: str | None, existing: str | None) -> None:
    """Restore a force backup after a failed download. The restore runs to
    completion even if a cancel lands meanwhile (its rmtree is an await), and
    the run gives the backup up only once the original is back — a failed
    restore stays with the run, so a later cancel retries and reports it."""
    fut = asyncio.ensure_future(_restore_backup(backup, existing))
    try:
        await run_to_completion(fut)
    finally:
        # Also on the cancel path: run_to_completion re-raises only after
        # the restore finished, so the result is in.
        ok = fut.done() and not fut.cancelled() and fut.result()
        if run is not None and (ok or backup is None):
            run.backup = None


async def _handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """✖ Cancel tapped on a status message (docs/cancel.md). Only the chat
    that requested the run may cancel it; the run's own task does the
    cleanup and writes the final text when the cancellation reaches it."""
    query = update.callback_query
    if not query or not query.data:
        return
    user_id = query.from_user.id if query.from_user else None
    if user_id not in config.ALLOWED_USERS:
        await query.answer("Not allowed.", show_alert=False)
        return
    cid = query.data.split(":", 1)[1] if ":" in query.data else ""
    run = _active_runs.get(cid)
    if run is None:
        await query.answer("Nothing to cancel — already finished.", show_alert=False)
        with contextlib.suppress(TelegramError):
            await query.edit_message_reply_markup(reply_markup=None)
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id != run.chat_id:
        await query.answer("Only the chat that requested this can cancel it.",
                           show_alert=True)
        return
    if run.cancel_requested:
        await query.answer("Cancelling…", show_alert=False)
        return
    run.cancel_requested = True
    logger.info("Cancel requested by chat %s for %s run %s", chat_id, run.kind, run.id)
    # Cancel first, acknowledge after: a failed query.answer() must not
    # leave the flag set with the task still running.
    if run.on_prompt_cancel is not None:
        await run.on_prompt_cancel()
    elif run.task is not None:
        run.task.cancel()
    # else: not attached yet — ActiveRun.attach() cancels on arrival
    with contextlib.suppress(TelegramError):
        await query.answer("Cancelling…", show_alert=False)


_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
