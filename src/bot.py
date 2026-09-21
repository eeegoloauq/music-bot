import asyncio
import contextlib
import errno
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
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.request import HTTPXRequest

from config import (
    TG_TOKEN, ALLOWED_USERS, MUSIC_DIR, NAVI_LOGIN, NAVI_PASS, NAVI_PUBLIC_URL,
    UPLOAD_HTTP_PORT,
)
import journal
import metadata
import reporting
import soulseek
from soulseek.downloader import run_to_completion
import navidrome
import retagger
import uploads
import upload_import
import upload_web
from inline import handle_inline_query, _DELETE_PREFIX
from library.files import _sanitize, _find_existing_track, _locate_existing_album

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

# Every album/track download, upload import and lossy prompt in flight,
# keyed by the short id in its ✖ Cancel button (docs/cancel.md).
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

# Re-tagger session state. One global slot — single-user bot. The session is
# a library snapshot, so library changes make it stale; the timer is only a
# safety net.
_RETAG_SESSION_TTL = 7200  # 2 hours
_retag_session: dict | None = None
_retag_invalidated_reason: str | None = None
_retag_in_progress = False


def _invalidate_retag_session(reason: str) -> None:
    global _retag_session, _retag_invalidated_reason
    if _retag_session is None:
        return
    _retag_session = None
    _retag_invalidated_reason = reason
    logger.info("Invalidated re-tag session: %s", reason)


def _purge_stale_lossy() -> None:
    now = time.monotonic()
    for cid in [k for k, v in _pending_lossy.items() if v["expire_at"] <= now]:
        entry = _pending_lossy.pop(cid, None)
        if entry and entry.get("run_id"):
            _active_runs.pop(entry["run_id"], None)


def _format_lossy_summary(candidates: list) -> str:
    """One-liner like 'mp3 320kbps from 5 peers' for the prompt UI."""
    mp3 = [c for c in candidates if c.extension == "mp3"]
    m4a = [c for c in candidates if c.extension == "m4a"]
    parts = []
    if mp3:
        # slskd's bitRate is already kbps (protocol units)
        best_kbps = max((c.bit_rate or 0) for c in mp3)
        peers = len({c.username for c in mp3})
        parts.append(f"mp3 {best_kbps}kbps from {peers} peer{'s' if peers != 1 else ''}")
    if m4a:
        peers = len({c.username for c in m4a})
        parts.append(f"m4a from {peers} peer{'s' if peers != 1 else ''}")
    return " · ".join(parts) if parts else f"{len(candidates)} lossy peers"


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


# Status messages are cosmetic: a Telegram hiccup on one must never abort
# filing/download work (docs/slow-source-recovery.md §B — a ~30s network blip
# on the "Importing upload…" edit once killed an import before any file was
# filed). Transient errors retry with backoff sized to outlive such a blip;
# final outcome messages retry harder and log at ERROR when lost, because
# losing one costs the user the report — the files are already safe on disk.
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
        f"/scan — trigger Navidrome library rescan\n"
        f"/sharescan — trigger heavy slskd share rescan\n\n"
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
        "Add <b>re</b> after the link to force re-download.\n"
        "Every download and upload import carries a ✖ Cancel button until it "
        "finishes — tap it at any stage; tracks already saved stay.\n\n"
        "<b>Inline mode</b>\n"
        f"<code>@{bot_me.username} song name</code> — search Deezer\n"
        f"<code>@{bot_me.username} np</code> — now playing as audio\n"
        f"<code>@{bot_me.username} s</code> — share link for current track\n"
        f"<code>@{bot_me.username} l</code> — lyrics for current track\n"
        f"<code>@{bot_me.username} lib name</code> — search library\n"
        f"<code>@{bot_me.username} del name</code> — delete album\n\n"
        "<b>Commands</b>\n"
        "/scan — trigger Navidrome library rescan\n"
        "/sharescan — trigger heavy slskd share rescan\n"
        "/stats — library statistics\n"
        "/retag — refresh tags on every album from Deezer + Last.fm "
        "(dry-run, then <code>/retag confirm</code>)",
        parse_mode="HTML",
    )


@authorized
async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = await _trigger_scan()
    await update.message.reply_text(note)


@authorized
async def cmd_share_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = await _trigger_scan(slskd_mode="immediate")
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


_CHANGE_LABEL_MAP = {
    "comment": "comment",
    "genres": "genres",
    "folder": "folder rename",
    "artist folder": "artist folder rename",
    "track count": "track count",
    "releasedate (FLAC)": "release date",
    "replaygain": "ReplayGain",
    "album/albumartist casing": "casing",
    "Last.fm only": "genres only",
}


def _pretty_change_label(raw: str) -> str:
    """Strip the data-only suffix off a raw change string and map known
    keys to human-readable labels."""
    head = raw.split(":", 1)[0].split("(", 1)[0].strip().rstrip(":")
    return _CHANGE_LABEL_MAP.get(head, head)


def _outcome_tag(plan) -> str:
    """One-token outcome label for a finished AlbumPlan."""
    if plan.error:
        return "skipped"
    if plan.is_lastfm_only:
        return "Last.fm only"
    if not plan.needs_apply:
        return "already canonical"
    return "Deezer match"


def _retag_progress_text(
    phase: str, idx: int, total: int, plan, started_at: float,
    tally_changed: int, tally_lastfm: int, tally_skipped: int,
) -> str:
    """Three-line dry-run progress: header + last item + running tally.

    Wording uses future tense (``to update``) since dry-run doesn't write
    anything until the user replies ``/retag confirm``.
    """
    pct = (idx * 100 // total) if total else 0
    elapsed = max(time.monotonic() - started_at, 0.001)
    avg = elapsed / max(idx, 1)
    eta = avg * max(total - idx, 0)
    head = f"{plan.artist_dir} / {plan.album_dir}" if plan else ""
    if len(head) > 60:
        head = head[:58] + "…"
    return (
        f"<b>{phase}</b> · {idx} / {total} · {pct}% · ETA {_format_eta(eta)}\n"
        f"last: {head} · {_outcome_tag(plan)}\n"
        f"to update <b>{tally_changed}</b> · "
        f"genre-only <b>{tally_lastfm}</b> · "
        f"skipped <b>{tally_skipped}</b>"
    )


def _retag_apply_progress_text(
    idx: int, total: int, plan, started_at: float,
    written_total: int, skipped_total: int,
) -> str:
    pct = (idx * 100 // total) if total else 0
    elapsed = max(time.monotonic() - started_at, 0.001)
    avg = elapsed / max(idx, 1)
    eta = avg * max(total - idx, 0)
    head = f"{plan.artist_dir} / {plan.album_dir}" if plan else ""
    if len(head) > 60:
        head = head[:58] + "…"
    last_files = getattr(plan, "files_written", 0) if plan else 0
    return (
        f"<b>Re-tag apply</b> · {idx} / {total} · {pct}% · ETA {_format_eta(eta)}\n"
        f"last: {head} · {last_files} files\n"
        f"total: <b>{written_total}</b> files updated · "
        f"<b>{skipped_total}</b> skipped"
    )


def _format_retag_summary(plans: list, summary, elapsed: float, sample_n: int = 8) -> str:
    """Dry-run summary in best-practice form: clear future-tense wording,
    sub-counts inline, top changes + not-found split into sections.
    """
    will_change = [p for p in plans if p.needs_apply]
    full_n = sum(1 for p in will_change if not p.is_lastfm_only)
    lastfm_n = sum(1 for p in will_change if p.is_lastfm_only)

    sample = will_change[:sample_n]
    sample_lines = []
    for p in sample:
        head = f"{p.artist_dir} / {p.album_dir}"
        if len(head) > 70:
            head = head[:68] + "…"
        labels = [_pretty_change_label(c) for c in p.changes[:4]]
        # Drop empty / dup labels conservatively
        seen = set()
        clean_labels = []
        for lbl in labels:
            if lbl and lbl not in seen:
                seen.add(lbl)
                clean_labels.append(lbl)
        tag = ", ".join(clean_labels)
        if len(p.changes) > 4:
            tag += "…"
        sample_lines.append(f"  {head} — {tag}")

    failed = [p for p in plans if p.error]
    failed_lines = []
    for p in failed[:5]:
        head = f"{p.artist_dir} / {p.album_dir}"
        if len(head) > 70:
            head = head[:68] + "…"
        # The search's own note ("edition differs…", "found as …") is the
        # useful part; the generic prefix is implied by the section title.
        note = (p.error or "").split(" — ", 1)
        if len(note) == 2:
            head += f" — {note[1]}"
        failed_lines.append(f"  {reporting.esc(head)}")

    out = [
        f"<b>Scan complete</b> · {summary.total} albums · {_format_eta(elapsed)}",
        "",
    ]
    if summary.will_change:
        # Compact two-piece breakdown when both Deezer + Last.fm-only branches
        # are populated; else just the bare count.
        if full_n and lastfm_n:
            out.append(
                f"<b>{summary.will_change}</b> will update — "
                f"{full_n} full + {lastfm_n} genre-only"
            )
        else:
            out.append(f"<b>{summary.will_change}</b> will update")
    if summary.no_changes:
        out.append(f"<b>{summary.no_changes}</b> already canonical")
    if summary.unidentified:
        out.append(f"<b>{summary.unidentified}</b> not found on Deezer")

    if sample_lines:
        out.append("")
        out.append(f"<b>Top updates</b> ({len(sample_lines)} of {summary.will_change}):")
        out.extend(sample_lines)
        if summary.will_change > sample_n:
            out.append(f"  +{summary.will_change - sample_n} more")
    if failed_lines:
        out.append("")
        out.append(f"<b>Not found</b> ({len(failed_lines)} of {len(failed)}):")
        out.extend(failed_lines)
        if len(failed) > 5:
            out.append(f"  +{len(failed) - 5} more")
    if summary.will_change:
        out.append("")
        out.append(
            "Send <code>/retag confirm</code> to apply or "
            "<code>/retag stop</code> to discard."
        )
    return "\n".join(out)


@authorized
async def cmd_retag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _retag_session, _retag_invalidated_reason, _retag_in_progress

    args = (context.args or [])
    sub = args[0].lower() if args else ""

    if sub == "stop":
        if _retag_session is None:
            await update.message.reply_text("No re-tag session pending.")
        else:
            _retag_session = None
            await update.message.reply_text("Re-tag session dropped.")
        _retag_invalidated_reason = None
        return

    if sub == "confirm":
        if _retag_session is None:
            if _retag_invalidated_reason:
                await update.message.reply_text(
                    "Library changed since the scan "
                    f"({_retag_invalidated_reason}) — run /retag again."
                )
            else:
                await update.message.reply_text(
                    "No re-tag session to confirm. Run /retag first."
                )
            return
        if time.monotonic() > _retag_session["expire_at"]:
            _retag_session = None
            _retag_invalidated_reason = None
            await update.message.reply_text(
                "Re-tag session expired (2 hours). Run /retag again."
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

    status_msg = await safe_send(update.message, "Scanning library…")
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
            await safe_edit(status_msg, f"Re-tag failed: {_short(e)}", final=True)
            return
    finally:
        _retag_in_progress = False

    elapsed = time.monotonic() - started
    _retag_session = {
        "plans": plans,
        "summary": summary,
        "expire_at": time.monotonic() + _RETAG_SESSION_TTL,
    }
    _retag_invalidated_reason = None
    text = _format_retag_summary(plans, summary, elapsed)
    if not await safe_edit(status_msg, text, final=True, parse_mode="HTML"):
        await _tg_call(lambda: update.message.reply_text(text, parse_mode="HTML"),
                       "re-tag summary", final=True)


async def _do_retag_apply(update: Update, plans: list):
    global _retag_session, _retag_in_progress
    _retag_in_progress = True
    status_msg = await safe_send(update.message, "Applying re-tag…")
    last_edit = [time.monotonic()]
    started = time.monotonic()
    written_total = [0]
    skipped_total = [0]
    last_plan_holder = [None]

    async def progress(idx, total, plan):
        last_plan_holder[0] = plan
        written_total[0] += getattr(plan, "files_written", 0)
        skipped_total[0] += getattr(plan, "files_skipped", 0)
        if time.monotonic() - last_edit[0] < 5 and idx != total:
            return
        last_edit[0] = time.monotonic()
        text = _retag_apply_progress_text(
            idx, total, plan, started,
            written_total[0], skipped_total[0],
        )
        try:
            await status_msg.edit_text(text, parse_mode="HTML")
        except TelegramError:
            pass

    try:
        try:
            stats = await retagger.run_apply(plans, progress=progress)
        except Exception as e:
            logger.exception("Re-tag apply failed")
            await safe_edit(status_msg, f"Apply failed: {_short(e)}", final=True)
            return
    finally:
        _retag_in_progress = False
        _retag_session = None

    elapsed = time.monotonic() - started
    out = [
        f"<b>Re-tag complete</b> · {stats['albums_planned']} albums · "
        f"{_format_eta(elapsed)}",
        "",
    ]
    parts = [f"<b>{stats['files_written']}</b> files updated"]
    if stats["files_skipped"]:
        parts.append(f"<b>{stats['files_skipped']}</b> skipped (no track match)")
    out.append(" · ".join(parts))

    if stats["failed"]:
        out.append("")
        out.append(f"<b>{len(stats['failed'])}</b> album-level failures:")
        for path, err in stats["failed"][:3]:
            out.append(f"  {path} — {_short(Exception(err))}")
        if len(stats["failed"]) > 3:
            out.append(f"  +{len(stats['failed']) - 3} more")

    out.append("")
    out.append(await _trigger_scan(slskd_mode="scheduled"))
    text = "\n".join(out)
    if not await safe_edit(status_msg, text, final=True, parse_mode="HTML"):
        await _tg_call(lambda: update.message.reply_text(text, parse_mode="HTML"),
                       "re-tag apply summary", final=True)


@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""

    if text.startswith(_DELETE_PREFIX):
        rel_path = text[len(_DELETE_PREFIX):].strip()
        if rel_path:
            await _handle_delete(update, rel_path)
        return

    # Match force words against the text with URLs removed — album/track slugs
    # like ".../album/re-animator" contain hyphen-delimited "re" that would
    # otherwise silently trigger a destructive re-download.
    force = bool(_FORCE_RE.search(_MUSIC_LINK_RE.sub(" ", text)))

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
    _invalidate_retag_session("album deleted")

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

    scan_note = await _trigger_scan(slskd_mode="scheduled")
    await update.message.reply_text(f"Deleted: {artist_name} — {album_name}\n{scan_note}")


async def _trigger_scan(*, slskd_mode: str = "none") -> str:
    """Trigger Navidrome library scan + slskd share rescan scheduling.

    Navidrome reflects new tags to playback users, so it runs immediately.
    slskd exposes only a full share scan and it is expensive; normal downloads
    skip it, delete/retag schedule a delayed quiet-period scan, and explicit
    /sharescan requests an immediate slskd scan.
    Returns a status line for the user.
    """
    if slskd_mode == "immediate":
        asyncio.create_task(soulseek.rescan_shares())
        slskd_note = "slskd share scan triggered."
    elif slskd_mode == "scheduled":
        delay = soulseek.schedule_rescan_shares()
        mins = max(1, round(delay / 60))
        slskd_note = f"slskd share scan scheduled in ~{mins}m."
    else:
        slskd_note = ""

    if not NAVI_LOGIN or not NAVI_PASS:
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


async def _resolve_and_download(update: Update, url: str, force: bool = False):
    status_msg = await update.message.reply_text("Resolving link…")
    try:
        result = await metadata.resolve_link(url)
    except Exception as e:
        logger.error("Odesli resolve failed for %s: %s", url, e)
        await status_msg.edit_text(f"❌ Failed to resolve link: {_short(e)}")
        return

    if result is None:
        await status_msg.edit_text("❌ Could not resolve link.")
        return

    link_type, album_or_track_id = result
    await status_msg.delete()

    if link_type == "album":
        await _download_album(update, album_or_track_id, force=force)
    else:
        await _download_track(update, album_or_track_id, force=force)


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


async def _download_album(update: Update, album_id: str, force: bool = False):
    io = ChatIO.from_update(update)
    _spawn_run(_run_album(io, album_id, force=force), io, f"album {album_id}")


async def _run_album(io: ChatIO, album_id: str, force: bool = False,
                     resume_entry: journal.PendingDownload | None = None) -> bool:
    """Accept → journal → download → report → clear. Shared by fresh
    requests and journal resumes. Returns False when the album is already
    in flight (caller decides whether to tell the user)."""
    key = f"album:{album_id}"
    run = ActiveRun(io.chat_id, "album")
    if key in _in_flight:
        run.close()
        return False
    _in_flight.add(key)
    run.attach()
    status_msg = None
    try:
        if _download_semaphore.locked():
            status_msg = await safe_send(
                io, "Queued — will start after the current download finishes",
                reply_markup=run.markup)
        else:
            status_msg = await safe_send(io, "Fetching album info…",
                                         reply_markup=run.markup)
        status_msg = RunStatus(status_msg, run)
        entry = resume_entry or journal.PendingDownload(
            kind="album", id=album_id, chat_id=io.chat_id, force=force)
        entry.status_message_id = status_msg.message_id
        journal.add(entry)
        await _do_download_album(io, status_msg, album_id, force=force)
        # _do_download_album reports every outcome — success and failure
        # alike — before returning, so the request leaves the crash ledger.
        # An escaped exception keeps the entry for the next startup's resume.
        journal.remove("album", album_id, io.chat_id)
        return True
    except asyncio.CancelledError:
        if not run.cancel_requested:
            raise                       # shutdown, not the user — stays journaled
        # The user's cancel is a reported outcome: swallow the cancellation,
        # tell them what landed, and clear the ledger so a restart doesn't
        # resurrect the request.
        asyncio.current_task().uncancel()
        await _report_cancelled(status_msg, run, io)
        journal.remove("album", album_id, io.chat_id)
        return True
    finally:
        _in_flight.discard(key)
        run.close()


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


async def _do_download_album(io: ChatIO, status_msg, album_id: str, force: bool = False):
    # The run (when the status message carries one) gets what the cancel
    # report needs; a CancelledError itself propagates to _run_album.
    run: ActiveRun | None = getattr(status_msg, "run", None)
    async with _download_semaphore:
        try:
            album = await metadata.fetch_album(album_id)
            await metadata.enrich_genres(album)
        except Exception as e:
            logger.error("Failed to fetch album %s: %s", album_id, e)
            await safe_edit(status_msg, f"❌ Failed to fetch album info: {_short(e)}",
                            final=True)
            return
        if run is not None:
            run.total = len(album["tracks"])

        # Force re-download: move the existing album OUT of the artist tree
        # into a hidden staging dir, then download fresh. It has to leave the
        # tree entirely: the downloader re-runs tag-based _locate_existing_album,
        # and a ".bak" sibling still carries our canonical comment, so an
        # in-tree backup gets re-selected as the target and every track is
        # skipped as "already present" — which then destroyed the only copy.
        # We locate by tag (not name) so a foreign-sanitised folder still moves.
        backup_dir = None
        restore_target = None
        if force:
            existing_dir = await asyncio.to_thread(_locate_existing_album, MUSIC_DIR, album)
            if existing_dir and os.path.isdir(existing_dir):
                backup_root = os.path.join(MUSIC_DIR, ".redownload-backup")
                os.makedirs(backup_root, exist_ok=True)
                restore_target = existing_dir
                backup_dir = os.path.join(backup_root, os.path.basename(existing_dir.rstrip("/")))
                if os.path.exists(backup_dir):
                    await asyncio.to_thread(shutil.rmtree, backup_dir)
                os.rename(existing_dir, backup_dir)
                logger.info("Force re-download: staged backup %s -> %s", existing_dir, backup_dir)
                if run is not None:
                    run.backup = (backup_dir, restore_target)

        prog = reporting.AlbumProgress(
            album["artist"], album["title"], len(album["tracks"]))
        if run is not None:
            run.saved_fn = lambda: prog.saved
        live = LiveStatus(
            edit_fn=lambda text: status_msg.edit_text(text, parse_mode="HTML"),
            render_fn=prog.render,
        )
        await live.refresh(force=True)

        async def on_event(ev: dict):
            await live.refresh(force=prog.handle(ev))

        try:
            result = await soulseek.download_album(
                album_id, MUSIC_DIR, album=album, on_event=on_event,
            )
        except Exception as e:
            logger.error("Album download failed (%s — %s): %s", album["artist"], album["title"], e)
            await _put_back(run, backup_dir, restore_target)
            await safe_edit(status_msg, f"❌ Download failed: {_short(e)}", final=True)
            return
        if run is not None:
            # Files are on disk: from here it's backup bookkeeping, scan and
            # report — nothing left to cancel, and the backup is this flow's.
            run.close()
            run.backup = None

        # Only drop the backup once the fresh copy is a clean, complete success.
        # A partial or empty re-download (dead peers, some tracks missing) must
        # NOT delete the known-good original — restore it and keep the backup net.
        restored = False
        if backup_dir and os.path.isdir(backup_dir):
            if result["downloaded"] > 0 and not result["failed"]:
                await asyncio.to_thread(shutil.rmtree, backup_dir)
                logger.info("Force re-download complete (%d saved), removed backup",
                            result["downloaded"])
            else:
                await _restore_backup(backup_dir, restore_target)
                restored = True
                logger.warning(
                    "Force re-download incomplete (%d saved, %d failed) — restored original",
                    result["downloaded"], len(result["failed"]),
                )

        if restored:
            # The fresh copy was discarded — report what actually happened
            # rather than the (now untrue) per-track download tally.
            got = result["downloaded"]
            miss = len(result["failed"])
            await safe_edit(
                status_msg,
                f"Kept existing: {album['artist']} — {album['title']}\n"
                f"Re-download was incomplete ({got} ok, {miss} unavailable), "
                f"so the original copy was restored.",
                final=True,
            )
            return

        scan_note = ""
        if result["downloaded"] > 0:
            _invalidate_retag_session("album downloaded")
            scan_note = await _trigger_scan()

        # Share link works even for "already in library" — useful when re-pasting old albums.
        share_url = await _try_share_album(
            album["artist"], album["title"], skip_delay=result["downloaded"] == 0)

        if result["downloaded"] == 0 and not result["failed"]:
            done_text = (
                f"<b>{reporting.esc(album['artist'])} — "
                f"{reporting.esc(album['title'])}</b>\nAlready in library."
            )
            if share_url:
                done_text += f"\n\n{share_url}"
        else:
            done_text = reporting.render_album_final(
                album["artist"], album["title"], result,
                scan_note=scan_note, share_url=share_url,
            )

        await _send_result(io, status_msg, done_text, result["album_dir"])


async def _handle_upload(io: ChatIO, report: uploads.IntakeReport) -> None:
    """Drive a staged local upload through the same metadata → tag → file →
    scan → report path a Soulseek download takes (docs/local-upload-plan.md)."""
    if report.error:
        await io.reply_text(uploads.format_rejection(report))
        return

    run = ActiveRun(io.chat_id, "upload")
    run.attach()
    status_msg = None
    try:
        status_msg = RunStatus(await safe_send(
            io, f"📦 {report.name}: {len(report.audio)} audio file(s) received. "
            "Identifying release…", reply_markup=run.markup), run)
        await _import_upload(io, status_msg, report, run)
    except asyncio.CancelledError:
        if not run.cancel_requested:
            raise
        # Staged files are the user's own — a cancel leaves them where a
        # failed import would: in uploads staging, nothing deleted.
        asyncio.current_task().uncancel()
        await _report_cancelled(status_msg, run, io)
    finally:
        run.close()


async def _import_upload(io: ChatIO, status_msg, report: uploads.IntakeReport,
                         run: ActiveRun) -> None:
    album_id = None
    diag: dict = {}
    try:
        album_id = await upload_import.identify_album(
            report.staging_dir, report.name, diag=diag)
    except Exception as e:
        logger.error("Upload identify failed for %s: %s", report.name, e)
    if not album_id:
        await safe_edit(
            status_msg,
            f"❓ {report.name}: couldn't match this to a release — "
            f"{upload_import.describe_identify_failure(diag)}. Files are "
            "kept in uploads staging; nothing was filed.", final=True)
        return

    if _download_semaphore.locked():
        logger.info("Upload release identified for %s; import queued until the "
                    "current download finishes", report.name)
        await safe_edit(
            status_msg,
            f"📦 {report.name}: release identified. Import queued until the "
            "current download finishes…")

    progress: dict = {"saved": 0}
    run.saved_fn = lambda: progress["saved"]
    async with _download_semaphore:
        try:
            album = await metadata.fetch_album(album_id)
            await metadata.enrich_genres(album)
            run.total = len(album["tracks"])
            # Cosmetic edit inside the critical path — safe_edit, never a
            # bare edit_text: a network blip here once aborted the whole
            # import before a single file was filed.
            await safe_edit(
                status_msg,
                f"📦 Importing upload: {album['artist']} — {album['title']}…")
            result = await upload_import.import_staged_album(
                album, report.staging_dir, MUSIC_DIR, progress=progress)
        except Exception as e:
            logger.exception("Upload import failed for %s", report.name)
            await safe_edit(
                status_msg,
                f"❌ Upload import failed: {_short(e)}. Files kept in staging.",
                final=True)
            return
    run.close()  # filed; scan and report are not cancellable

    scan_note = ""
    if result["downloaded"]:
        _invalidate_retag_session("upload imported")
        scan_note = await _trigger_scan()
    share_url = await _try_share_album(
        album["artist"], album["title"], skip_delay=result["downloaded"] == 0)

    if result["downloaded"] == 0 and not result["failed"]:
        done_text = (
            f"<b>{reporting.esc(album['artist'])} — "
            f"{reporting.esc(album['title'])}</b>\nAlready in library — "
            "the uploaded copy was dropped as a duplicate."
        )
        if share_url:
            done_text += f"\n\n{share_url}"
    else:
        done_text = reporting.render_album_final(
            album["artist"], album["title"], result,
            scan_note=scan_note, share_url=share_url,
        )
    leftovers = result.get("leftover_files") or []
    if leftovers:
        shown = ", ".join(reporting.esc(os.path.basename(f)) for f in leftovers[:3])
        more = f" +{len(leftovers) - 3} more" if len(leftovers) > 3 else ""
        done_text += f"\n\n⚠️ Didn't match any track, left in staging: {shown}{more}"
    await _send_result(io, status_msg, done_text, result["album_dir"])


async def _download_track(update: Update, track_id: str, force: bool = False):
    io = ChatIO.from_update(update)
    _spawn_run(_run_track(io, track_id, force=force), io, f"track {track_id}")


async def _run_track(io: ChatIO, track_id: str, force: bool = False,
                     resume_entry: journal.PendingDownload | None = None) -> bool:
    """Track twin of _run_album — same accept → journal → report → clear
    bracket. A pending lossy-fallback prompt counts as a reported outcome:
    prompts don't survive restarts by design."""
    key = f"track:{track_id}"
    run = ActiveRun(io.chat_id, "track")
    if key in _in_flight:
        run.close()
        return False
    _in_flight.add(key)
    run.attach()
    status_msg = None
    try:
        if _download_semaphore.locked():
            status_msg = await safe_send(
                io, "Queued — will start after the current download finishes",
                reply_markup=run.markup)
        else:
            status_msg = await safe_send(io, "Fetching track info…",
                                         reply_markup=run.markup)
        status_msg = RunStatus(status_msg, run)
        entry = resume_entry or journal.PendingDownload(
            kind="track", id=track_id, chat_id=io.chat_id, force=force)
        entry.status_message_id = status_msg.message_id
        journal.add(entry)
        await _do_download_track(io, status_msg, track_id, force=force)
        journal.remove("track", track_id, io.chat_id)
        return True
    except asyncio.CancelledError:
        if not run.cancel_requested:
            raise
        asyncio.current_task().uncancel()
        await _report_cancelled(status_msg, run, io)
        journal.remove("track", track_id, io.chat_id)
        return True
    finally:
        _in_flight.discard(key)
        run.close()


async def _do_download_track(io: ChatIO, status_msg, track_id: str, force: bool = False):
    run: ActiveRun | None = getattr(status_msg, "run", None)
    async with _download_semaphore:
        try:
            track, album_ctx = await metadata.fetch_single_track(track_id)
            if album_ctx:
                await metadata.enrich_genres(album_ctx)
        except Exception as e:
            logger.error("Failed to fetch track %s: %s", track_id, e)
            await safe_edit(status_msg, f"❌ Failed to fetch track info: {_short(e)}",
                            final=True)
            return

        backup = existing = None
        if force:
            album_dir = _locate_existing_album(MUSIC_DIR, album_ctx)
            existing = _find_existing_track(album_dir, track) if album_dir else None
            if existing:
                # Aside, not gone: a cancel (or a failed download) puts it
                # back; a clean success drops it below.
                backup = existing + ".redownload-backup"
                os.replace(existing, backup)
                logger.info("Force re-download: staged backup %s -> %s", existing, backup)
                if run is not None:
                    run.backup = (backup, existing)

        prog = reporting.TrackProgress(track["artist"], track["title"])
        if run is not None:
            run.saved_fn = lambda: prog.saved
        live = LiveStatus(
            edit_fn=lambda text: status_msg.edit_text(text, parse_mode="HTML"),
            render_fn=prog.render,
        )
        await live.refresh(force=True)

        async def on_event(ev: dict):
            await live.refresh(force=prog.handle(ev))

        try:
            path, was_downloaded, fmt = await soulseek.download_single_track(
                track, album_ctx, MUSIC_DIR, on_event=on_event,
            )
        except RuntimeError as e:
            if "No FLAC found" in str(e):
                outcome = await _offer_mp3_fallback(status_msg, track_id, track, album_ctx)
                if outcome == "prompt":
                    # The prompt owns the outcome now; the original is gone
                    # either way (force semantics), so drop the backup.
                    if run is not None:
                        run.backup = None
                    _drop_backup(backup)
                    return
                await _put_back(run, backup, existing)
                if outcome == "reported":
                    return
                await safe_edit(
                    status_msg,
                    f"❌ No FLAC or acceptable lossy fallback for "
                    f"{track['artist']} — {track['title']}",
                    final=True,
                )
                return
            await _put_back(run, backup, existing)
            logger.error("Track download failed (%s — %s): %s", track["artist"], track["title"], e)
            await safe_edit(status_msg, f"❌ Download failed: {_short(e)}", final=True)
            return
        except Exception as e:
            await _put_back(run, backup, existing)
            logger.error("Track download failed (%s — %s): %s", track["artist"], track["title"], e)
            await safe_edit(status_msg, f"❌ Download failed: {_short(e)}", final=True)
            return
        if run is not None:
            run.close()  # saved; scan and report are not cancellable
            run.backup = None
        _drop_backup(backup)

        scan_note = ""
        if was_downloaded:
            _invalidate_retag_session("track downloaded")
            scan_note = await _trigger_scan()

        share_url = await _try_share_album(
            album_ctx.get("artist", track["artist"]),
            album_ctx.get("title", ""),
            skip_delay=not was_downloaded,
        )

        if was_downloaded:
            done_text = reporting.render_track_final(
                track["artist"], track["title"], fmt, prog.peer,
                time.monotonic() - prog.started,
                scan_note=scan_note, share_url=share_url,
            )
        else:
            done_text = (
                f"<b>{reporting.esc(track['artist'])} — "
                f"{reporting.esc(track['title'])}</b>\nAlready in library."
            )
            if share_url:
                done_text += f"\n\n{share_url}"

        await _send_result(io, status_msg, done_text, os.path.dirname(path))


async def _offer_mp3_fallback(
    status_msg, track_id: str, track: dict, album_ctx: dict,
) -> str | None:
    """Probe Soulseek for mp3/m4a candidates after a FLAC search came up empty.
    If any are available, send an inline-keyboard prompt and stash the
    candidates for the callback handler. Returns True when a prompt was sent
    (caller leaves status_msg alone), False when there's nothing to fall back
    to (caller writes the final failure message).
    """
    _purge_stale_lossy()
    try:
        candidates = await soulseek.find_lossy_candidates(track, album_ctx)
    except soulseek.SearchError as e:
        # Searches are being throttled or slskd is down — "no fallback found"
        # would be a lie. Report the real reason and stop here.
        logger.warning("Lossy fallback search couldn't run: %s", e)
        reason = "rate-limited" if isinstance(e, soulseek.SearchThrottledError) \
            else "search error"
        await safe_edit(
            status_msg,
            f"Soulseek search couldn't run for {track['artist']} — "
            f"{track['title']} ({reason}). Try again in a few minutes.",
            final=True,
        )
        return "reported"
    except Exception as e:
        logger.warning("Lossy fallback search failed: %s", e)
        return None
    if not candidates:
        return None

    cid = uuid.uuid4().hex[:12]
    # The search run is over; the prompt is a run of its own so ✖ Cancel
    # keeps working while the user decides (it acts like Skip).
    old_run: ActiveRun | None = getattr(status_msg, "run", None)
    if old_run is not None:
        old_run.close()
    prompt_run = ActiveRun(status_msg.chat_id, "prompt")

    async def _cancel_prompt() -> None:
        _pending_lossy.pop(cid, None)
        prompt_run.close()
        await safe_edit(
            status_msg,
            f"✖ Cancelled — FLAC not available for {track['artist']} — "
            f"{track['title']}, no fallback taken", final=True)

    prompt_run.on_prompt_cancel = _cancel_prompt
    _pending_lossy[cid] = {
        "track_id": track_id,
        "track": track,
        "album_ctx": album_ctx,
        "candidates": candidates,
        "chat_id": status_msg.chat_id,
        "message_id": status_msg.message_id,
        "expire_at": time.monotonic() + _LOSSY_PROMPT_TTL,
        "run_id": prompt_run.id,
    }

    summary = _format_lossy_summary(candidates)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✓ Accept {summary}", callback_data=f"lossy:accept:{cid}")],
        [InlineKeyboardButton("✗ Skip", callback_data=f"lossy:skip:{cid}")],
        prompt_run.cancel_row(),
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
        prompt_run.close()
        return None
    return "prompt"


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
    _active_runs.pop(entry.get("run_id", ""), None)  # the prompt's ✖ Cancel

    await query.answer()

    track = entry["track"]

    if action == "skip":
        with contextlib.suppress(TelegramError):
            await query.edit_message_text(
                f"Skipped — FLAC not available for {track['artist']} — {track['title']}"
            )
        return

    if action != "accept":
        return

    # Same reason as _spawn_run: the download must not sit inside the
    # callback handler, or no further update (✖ Cancel included) gets
    # processed until it ends.
    _spawn_background(_run_lossy_download(query, entry))


async def _run_lossy_download(query, entry: dict) -> None:
    track = entry["track"]
    album_ctx = entry["album_ctx"]
    candidates = entry["candidates"]
    chat_id = entry.get("chat_id")

    in_flight_key = f"track:{entry['track_id']}:lossy"
    if in_flight_key in _in_flight:
        with contextlib.suppress(TelegramError):
            await query.edit_message_text("Already queued.")
        return
    _in_flight.add(in_flight_key)
    run = ActiveRun(chat_id, "track")
    run.attach()
    status_msg = RunStatus(_QueryMessage(query), run)
    try:
        summary = _format_lossy_summary(candidates)
        await safe_edit(
            status_msg, f"Downloading: {track['artist']} — {track['title']} ({summary})")
        async with _download_semaphore:
            prog = reporting.TrackProgress(track["artist"], track["title"])
            run.saved_fn = lambda: prog.saved
            live = LiveStatus(
                edit_fn=lambda text: status_msg.edit_text(text, parse_mode="HTML"),
                render_fn=prog.render,
            )

            async def on_event(ev: dict):
                await live.refresh(force=prog.handle(ev))

            try:
                path, was_downloaded, fmt = await soulseek.download_single_track(
                    track, album_ctx, MUSIC_DIR,
                    accept_lossy=True,
                    precomputed_candidates=candidates,
                    on_event=on_event,
                )
            except Exception as e:
                logger.error(
                    "Lossy download failed (%s — %s): %s",
                    track["artist"], track["title"], e,
                )
                await safe_edit(status_msg, f"❌ Lossy download failed: {_short(e)}",
                                final=True)
                return
            run.close()  # saved; scan and report are not cancellable

            if was_downloaded:
                _invalidate_retag_session("lossy track downloaded")
            scan_note = await _trigger_scan()
            share_url = await _try_share_album(
                album_ctx.get("artist", track["artist"]),
                album_ctx.get("title", ""),
                skip_delay=False,
            )
            done_text = reporting.render_track_final(
                track["artist"], track["title"], fmt, prog.peer,
                time.monotonic() - prog.started,
                scan_note=scan_note, share_url=share_url,
            )
            await safe_edit(status_msg, done_text, final=True, parse_mode="HTML")
    except asyncio.CancelledError:
        if not run.cancel_requested:
            raise
        asyncio.current_task().uncancel()
        await _report_cancelled(status_msg, run)
    except Exception:
        logger.exception("Lossy download task died (%s — %s)",
                         track["artist"], track["title"])
    finally:
        _in_flight.discard(in_flight_key)
        run.close()


async def _handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """✖ Cancel tapped on a status message (docs/cancel.md). Only the chat
    that requested the run may cancel it; the run's own task does the
    cleanup and writes the final text when the cancellation reaches it."""
    query = update.callback_query
    if not query or not query.data:
        return
    user_id = query.from_user.id if query.from_user else None
    if user_id not in ALLOWED_USERS:
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


# asyncio keeps only weak references to tasks — a long-running startup task
# (the resume driver in particular) must be strongly held or it can be
# garbage-collected mid-execution.
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _resume_pending(app: Application) -> None:
    """Re-issue downloads whose outcome the user never saw — the bot was
    restarted (deploy, crash, reboot) mid-download. Tag-based dedup skips
    tracks that already landed; the attach-first enqueue converges on
    transfers slskd kept running through the restart.

    ``force`` is deliberately NOT resumed: a force re-run would re-stage the
    half-written fresh copy over the original's backup in
    ``.redownload-backup`` — a plain resume just fills in what's missing.
    """
    entries = journal.load()
    if not entries:
        return
    logger.info("Journal: %d pending download(s) to resume", len(entries))
    for entry in sorted(entries, key=lambda e: e.requested_at):
        if entry.resume_attempts >= journal.MAX_RESUME_ATTEMPTS:
            journal.remove(entry.kind, entry.id, entry.chat_id)
            logger.warning("Journal: giving up on %s %s after %d resume attempts",
                           entry.kind, entry.id, entry.resume_attempts)
            with contextlib.suppress(TelegramError):
                await app.bot.send_message(
                    entry.chat_id,
                    "A download kept dying across bot restarts — giving up on it. "
                    "Re-send the link to try again.",
                )
            continue
        journal.bump_attempts(entry)
        if entry.status_message_id:
            with contextlib.suppress(TelegramError):
                await app.bot.edit_message_text(
                    "🔁 Bot restarted — resuming this download…",
                    chat_id=entry.chat_id, message_id=entry.status_message_id,
                )
        io = ChatIO(app.bot, entry.chat_id)
        runner = _run_album if entry.kind == "album" else _run_track
        try:
            await _await_in_own_task(runner(io, entry.id, resume_entry=entry))
        except Exception as e:
            # Entry stays in the journal (bumped) — next restart retries
            # until the cap; the failure itself is already logged/reported
            # by the flow where possible.
            logger.error("Resume of %s %s failed: %s", entry.kind, entry.id, e)


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("help", "Show all features"),
        BotCommand("scan", "Trigger Navidrome library rescan"),
        BotCommand("sharescan", "Trigger heavy slskd share rescan"),
        BotCommand("stats", "Library statistics"),
        BotCommand("retag", "Re-tag library from current Deezer + Last.fm metadata"),
    ])
    # Reclaim space from prior failed sessions — partial files left in
    # the slskd staging dir would otherwise pile up forever. Async so the
    # bot is responsive even on a slow fs scan.
    _spawn_background(soulseek.cleanup_orphan_staging())
    # Pick up downloads a restart orphaned. Best-effort and serialized on
    # the download semaphore like any other request.
    _spawn_background(_resume_pending(app))
    # Local-upload intake: watch /data/uploads for dropped zips/folders,
    # then identify/tag/file each one like a normal download, reporting to
    # the owner (first ALLOWED_USERS id).
    if ALLOWED_USERS:
        owner_io = ChatIO(app.bot, ALLOWED_USERS[0])

        async def _on_upload(report: uploads.IntakeReport) -> None:
            with contextlib.suppress(TelegramError):
                await _await_in_own_task(_handle_upload(owner_io, report))

        _spawn_background(uploads.watch_loop(_on_upload))
    # One-page upload site feeding the same watched folder (off unless
    # UPLOAD_HTTP_PORT is set).
    if UPLOAD_HTTP_PORT:
        await upload_web.start(UPLOAD_HTTP_PORT)


async def _shutdown(app: Application) -> None:
    await metadata.close()
    await soulseek.close()
    await navidrome.close()
    await upload_web.stop()


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
    app.add_handler(CommandHandler("sharescan", cmd_share_scan))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("retag", cmd_retag))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(InlineQueryHandler(handle_inline_query))
    app.add_handler(CallbackQueryHandler(_handle_lossy_callback, pattern=r"^lossy:"))
    app.add_handler(CallbackQueryHandler(_handle_cancel_callback, pattern=r"^cancel:"))
    app.add_error_handler(_error_handler)
    return app


def _require_writable_volumes() -> None:
    """Stop with an instruction if the volumes still belong to someone else.

    The containers no longer run as root, so an install that predates that
    change has root-owned volumes. Without this the bot would start, log
    nothing unusual, and silently drop every download and journal write.
    A probe file is used rather than os.access, which answers about the uid
    and not about the mount.
    """
    for label, path in (("state", os.path.dirname(journal.JOURNAL_PATH) or "/data"),
                        ("library", MUSIC_DIR)):
        probe = os.path.join(path, f".write-check-{os.getpid()}")
        try:
            with open(probe, "w"):
                pass
        except OSError as exc:
            logger.error("Cannot write to the %s directory %s as uid=%d gid=%d: %s",
                         label, path, os.geteuid(), os.getegid(), exc)
            # A full disk is not a permission problem, and pointing at the
            # migration script would send the reader down the wrong path.
            if exc.errno in (errno.EACCES, errno.EPERM):
                logger.error("On the host, stop the stack and run "
                             "scripts/migrate-permissions.sh from the directory "
                             "holding compose.yaml, then start it again.")
            raise SystemExit(1)
        finally:
            try:
                os.remove(probe)
            except OSError:
                pass


def main():
    # The library is shared: the bot, slskd and Samba all write to it and
    # Navidrome reads it. Group write has to be there the moment a file is
    # created rather than patched in afterwards — a non-root process cannot
    # chmod a path another writer owns, so any such chmod would only ever be
    # papering over a mode that was already wrong. slskd gets the same value
    # through SLSKD_UMASK in compose.
    os.umask(0o002)
    _require_writable_volumes()

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
