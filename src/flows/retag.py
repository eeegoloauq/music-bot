"""Retag scan and confirmation sessions with progress reporting."""

import asyncio
import logging
import os
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from flows import state, common, library_actions
import config
import maintenance
import reporting
import retagger

logger = logging.getLogger(__name__)


_RETAG_SESSION_TTL = 7200  # 2 hours


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
        sample_lines.append(f"  {reporting.esc(head)} — {reporting.esc(tag)}")

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


def _retag_snapshot(plans: list) -> tuple:
    """Detect file additions, edits and replacements between preview and apply."""
    entries = []
    for plan in plans:
        for root, dirs, files in os.walk(plan.folder):
            dirs.sort()
            for name in sorted(files):
                path = os.path.join(root, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    entries.append((path, "unavailable"))
                else:
                    entries.append((path, stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return tuple(entries)


def _maintenance_picker(selection: dict):
    token = selection["token"]
    rows = []
    for name in maintenance.FIELDS:
        mode = selection["fields"].get(name, 0)
        label = ("replace existing" if mode == 2 else "fill missing") if name in {"lyrics", "artwork"} else "on"
        text = f"{'✓' if mode else '○'} {name.title()}" + (f": {label}" if mode else "")
        rows.append([InlineKeyboardButton(text, callback_data=f"maintain:{name}:{token}")])
    rows.append([InlineKeyboardButton("Preview changes", callback_data=f"maintain:preview:{token}"),
                 InlineKeyboardButton("Close", callback_data=f"maintain:close:{token}")])
    return InlineKeyboardMarkup(rows)


async def _show_maintenance_picker(update: Update, context, folder: str | None = None):
    selection = {"token": uuid.uuid4().hex[:12], "chat": update.effective_chat.id,
                 "owner": update.effective_user.id,
                 "folder": folder, "fields": {}, "expires": time.monotonic() + _RETAG_SESSION_TTL}
    context.user_data["maintenance_selection"] = selection
    scope = os.path.basename(folder) if folder else "Entire library"
    await update.effective_message.reply_text(
        f"Update fields · {scope}\nChoose fields, then preview. "
        "Tap Lyrics or Artwork again to allow replacement of existing content.",
        reply_markup=_maintenance_picker(selection))


@common.authorized
async def _handle_maintenance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _prefix, action, token = query.data.split(":", 2)
    selection = context.user_data.get("maintenance_selection")
    if (not selection or selection["token"] != token or selection["chat"] != update.effective_chat.id
            or selection["owner"] != update.effective_user.id
            or selection["expires"] < time.monotonic()):
        await query.answer("Selection expired. Use /maintain or reopen the album.", show_alert=True)
        return
    if action in maintenance.FIELDS:
        modes = 3 if action in {"lyrics", "artwork"} else 2
        selection["fields"][action] = (selection["fields"].get(action, 0) + 1) % modes
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=_maintenance_picker(selection))
    elif action == "close":
        context.user_data.pop("maintenance_selection", None)
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
    elif action == "preview":
        fields = frozenset(name for name, mode in selection["fields"].items() if mode)
        if not fields:
            await query.answer("Choose at least one field.", show_alert=True)
            return
        folder = selection["folder"]
        if folder and not library_actions._safe_library_album(folder):
            await query.answer("Album unavailable. Use /library again.", show_alert=True)
            return
        await query.answer()
        options = maintenance.Options(fields, selection["fields"].get("lyrics") == 2,
                                       selection["fields"].get("artwork") == 2)
        await _retag_command(update, [], folder=folder, maintenance_options=options)


@common.authorized
async def _handle_retag_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _prefix, action, token = query.data.split(":", 2)
    await query.answer()
    if not state._retag_session or state._retag_session.get("token") != token:
        await query.message.reply_text("This preview is no longer available.")
        return
    await _retag_command(update, [action])


def _format_maintenance_preview(plans, summary, options) -> str:
    updates = [update for plan in plans for update in plan.updates]
    counts = []
    for name in maintenance.FIELDS:
        if name in options.fields:
            keys = {"title"} if name == "titles" else {"artwork", "cover_file"} if name == "artwork" else {name}
            counts.append(f"{name}: {sum(bool(keys.intersection(u.values)) for u in updates)}")
    lines = ["<b>Selected field preview</b>", reporting.esc(options.describe()),
             f"{summary.total} albums · {len(updates)} files to update", ", ".join(counts)]
    details = [f"{p.artist_dir} / {p.album_dir} · {change}" for p in plans for change in p.changes]
    for index, detail in enumerate(details):
        line = reporting.esc(detail[:300])
        if sum(map(len, lines)) + len(line) > 2800:
            lines.append(f"+{len(details) - index} more changes")
            break
        lines.append(line)
    notes = [f"{p.artist_dir} / {p.album_dir}: {note}" for p in plans for note in p.notes]
    if summary.unidentified:
        lines.append(f"{summary.unidentified} albums could not be identified; skipped.")
    if notes:
        lines.append(f"<b>Skipped or unavailable: {len(notes)}</b>")
        for note in notes[:3]:
            line = reporting.esc(note[:180])
            if sum(map(len, lines)) + len(line) < 3500:
                lines.append(line)
    if updates:
        lines.append("Confirm to apply these values, or discard. Preview expires in 2 hours.")
    else:
        lines.append("No applicable updates. Files were left unchanged.")
    return "\n".join(lines)


async def _retag_command(update: Update, args: list[str], folder: str | None = None,
                         maintenance_options: maintenance.Options | None = None):
    message = update.effective_message
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if args and args[0].lower() in {"confirm", "stop"} and state._retag_session:
        if (state._retag_session.get("owner", user_id) != user_id
                or state._retag_session.get("chat", chat_id) != chat_id):
            await message.reply_text("This preview belongs to another user or chat.")
            return
    sub = args[0].lower() if args else ""

    if sub == "stop":
        if state._retag_session is None:
            await message.reply_text("No re-tag session pending.")
        else:
            state._retag_session = None
            await message.reply_text("Re-tag session dropped.")
        state._retag_invalidated_reason = None
        return

    if sub == "confirm":
        if state._retag_session is None:
            if state._retag_invalidated_reason:
                await message.reply_text(
                    "Library changed since the scan "
                    f"({state._retag_invalidated_reason}) — run /retag again."
                )
            else:
                await message.reply_text(
                    "No re-tag session to confirm. Run /retag first."
                )
            return
        if time.monotonic() > state._retag_session["expire_at"]:
            state._retag_session = None
            state._retag_invalidated_reason = None
            await message.reply_text(
                "Re-tag session expired (2 hours). Run /retag again."
            )
            return
        if state._retag_in_progress:
            await message.reply_text("Re-tag already running.")
            return
        snapshot = state._retag_session.get("snapshot")
        if snapshot is not None and snapshot != await asyncio.to_thread(
                _retag_snapshot, state._retag_session["plans"]):
            state._invalidate_retag_session("album files changed")
            await message.reply_text("Album files changed since the preview. Run /retag again.")
            return
        plans = state._retag_session["plans"]
        await _do_retag_apply(update, plans)
        return

    if state._retag_in_progress:
        await message.reply_text("Re-tag already running.")
        return

    if state._retag_session is not None:
        await message.reply_text("A preview is pending. Confirm it or use /retag stop first.")
        return
    status_msg = await common.safe_send(message, "Previewing album…" if folder else "Scanning library…")
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

    state._retag_in_progress = True
    try:
        try:
            options = {}
            if folder is not None:
                options["albums"] = [(folder, os.path.basename(os.path.dirname(folder)),
                                      os.path.basename(folder))]
            async with state._download_semaphore:
                if maintenance_options:
                    albums = options.get("albums")
                    if albums is None:
                        albums = await asyncio.to_thread(retagger.scan_albums, config.MUSIC_DIR)
                    albums = [entry for entry in albums if library_actions._safe_library_album(entry[0])]
                    plans, summary = await maintenance.preview(albums, maintenance_options, progress)
                else:
                    plans, summary = await retagger.run_dry_run(config.MUSIC_DIR, progress=progress, **options)
                snapshot = await asyncio.to_thread(_retag_snapshot, plans)
        except Exception as e:
            logger.exception("Re-tag dry-run failed")
            await common.safe_edit(status_msg, f"Re-tag failed: {common._short(e)}", final=True)
            return
    finally:
        state._retag_in_progress = False

    elapsed = time.monotonic() - started
    state._retag_session = {
        "owner": user_id,
        "chat": chat_id,
        "token": uuid.uuid4().hex[:12],
        "snapshot": snapshot,
        "plans": plans,
        "summary": summary,
        "maintenance": maintenance_options,
        "expire_at": time.monotonic() + _RETAG_SESSION_TTL,
    }
    state._retag_invalidated_reason = None
    text = _format_retag_summary(plans, summary, elapsed)
    if maintenance_options:
        text = _format_maintenance_preview(plans, summary, maintenance_options)
    if folder and not maintenance_options:
        details = [reporting.esc(change) for plan in plans for change in plan.changes]
        if details:
            text += "\n\n" + "\n".join(details)
        if len(text) > 3900:
            text = _format_retag_summary(plans, summary, elapsed)
    token = state._retag_session["token"]
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm", callback_data=f"retag:confirm:{token}"),
        InlineKeyboardButton("Discard", callback_data=f"retag:stop:{token}"),
    ]]) if any(p.needs_apply for p in plans) else None
    if markup is None:
        state._retag_session = None
    if not await common.safe_edit(status_msg, text, final=True, parse_mode="HTML", reply_markup=markup):
        await common._tg_call(lambda: message.reply_text(text, parse_mode="HTML", reply_markup=markup),
                       "re-tag summary", final=True)


async def _do_retag_apply(update: Update, plans: list):
    state._retag_in_progress = True
    status_msg = await common.safe_send(update.effective_message, "Applying re-tag…")
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
            async with state._download_semaphore:
                session = state._retag_session
                if session is None or (session.get("snapshot") is not None
                        and session["snapshot"] != await asyncio.to_thread(_retag_snapshot, plans)):
                    await common.safe_edit(status_msg, "Library changed since the preview. Run /retag again.", final=True)
                    return
                if session.get("maintenance"):
                    stats = await retagger.run_apply(plans, progress=progress, apply_fn=maintenance.apply_plan)
                else:
                    stats = await retagger.run_apply(plans, progress=progress)
        except Exception as e:
            logger.exception("Re-tag apply failed")
            await common.safe_edit(status_msg, f"Apply failed: {common._short(e)}", final=True)
            return
    finally:
        state._retag_in_progress = False
        state._retag_session = None

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
        out.append(f"<b>{len(stats['failed'])}</b> failures:")
        for path, err in stats["failed"][:3]:
            out.append(f"  {reporting.esc(path)} — {reporting.esc(common._short(Exception(err)))}")
        if len(stats["failed"]) > 3:
            out.append(f"  +{len(stats['failed']) - 3} more")

    out.append("")
    out.append(await library_actions._trigger_scan(slskd_mode="scheduled"))
    text = "\n".join(out)
    if not await common.safe_edit(status_msg, text, final=True, parse_mode="HTML"):
        await common._tg_call(lambda: update.effective_message.reply_text(text, parse_mode="HTML"),
                       "re-tag apply summary", final=True)
