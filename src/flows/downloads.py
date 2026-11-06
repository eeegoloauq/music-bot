"""Resolve, download, resume, and offer lossy fallback for music links."""

import asyncio
import contextlib
import logging
import os
import shutil
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

from flows import state, common, library_actions
from library.files import _find_existing_track, _locate_existing_album
import config
import journal
import metadata
import providers
import reporting

logger = logging.getLogger(__name__)


_LOSSY_PROMPT_TTL = 300  # seconds


_pending_lossy: dict[str, dict] = {}


def _purge_stale_lossy() -> None:
    now = time.monotonic()
    for cid in [k for k, v in _pending_lossy.items() if v["expire_at"] <= now]:
        entry = _pending_lossy.pop(cid, None)
        if entry and entry.get("run_id"):
            common._active_runs.pop(entry["run_id"], None)


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


async def _prepare_download(io: "ChatIO", kind: str, release_id: str,
                            force: bool = False) -> bool:
    """Identify and check local files before accepting any provider work."""
    try:
        if kind == "album":
            album = await metadata.fetch_album(release_id)
            tracks = album.get("tracks", [])
            artist, title = album["artist"], album["title"]
        else:
            track, album = await metadata.fetch_single_track(release_id)
            tracks = [track]
            artist, title = track["artist"], track["title"]

        def local_count():
            album_dir = _locate_existing_album(config.MUSIC_DIR, album or {}, canonicalize=False)
            return sum(bool(_find_existing_track(album_dir, t)) for t in tracks) if album_dir else 0

        present = await asyncio.to_thread(local_count)
    except Exception as exc:
        logger.warning("Release lookup failed for %s %s: %s", kind, release_id, exc)
        await io.reply_text(f"❌ Failed to identify release: {common._short(exc)}")
        return False
    label = f"{artist} — {title}"
    if tracks and present == len(tracks) and not force:
        await io.reply_text(f"{label}\nAlready in library.")
        return False
    status = await providers.check()
    if status.ready:
        return True
    count = f"\nIn library: {present}/{len(tracks)} tracks." if tracks else ""
    await io.reply_text(
        f"{label}{count}\n{status.message}\n"
        "Import files or check Providers, then send the link again.",
        reply_markup=common._provider_actions())
    return False


async def _resolve_and_download(update: Update, url: str, force: bool = False):
    status_msg = await update.message.reply_text("Resolving link…")
    try:
        result = await metadata.resolve_link(url)
    except Exception as e:
        logger.error("Resolve failed for %s: %s", url, e)
        await status_msg.edit_text(f"❌ Failed to resolve link: {common._short(e)}")
        return

    if result is None:
        await status_msg.edit_text(
            f"❌ Could not resolve link. Search by name instead: @{update.get_bot().username} artist title")
        return

    link_type, album_or_track_id = result
    await status_msg.delete()

    if link_type == "album":
        await _download_album(update, album_or_track_id, force=force)
    else:
        await _download_track(update, album_or_track_id, force=force)


async def _download_album(update: Update, album_id: str, force: bool = False):
    io = common.ChatIO.from_update(update)
    if not await _prepare_download(io, "album", album_id, force):
        return
    common._spawn_run(_run_album(io, album_id, force=force), io, f"album {album_id}")


async def _run_album(io: common.ChatIO, album_id: str, force: bool = False,
                     resume_entry: journal.PendingDownload | None = None) -> bool:
    """Accept → journal → download → report → clear. Shared by fresh
    requests and journal resumes. Returns False when the album is already
    in flight (caller decides whether to tell the user)."""
    key = f"album:{album_id}"
    run = common.ActiveRun(io.chat_id, "album")
    if key in state._in_flight:
        run.close()
        return False
    state._in_flight.add(key)
    run.attach()
    status_msg = None
    try:
        if state._download_semaphore.locked():
            status_msg = await common.safe_send(
                io, "Queued — will start after the current download finishes",
                reply_markup=run.markup)
        else:
            status_msg = await common.safe_send(io, "Fetching album info…",
                                         reply_markup=run.markup)
        status_msg = common.RunStatus(status_msg, run)
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
        await common._report_cancelled(status_msg, run, io)
        journal.remove("album", album_id, io.chat_id)
        return True
    finally:
        state._in_flight.discard(key)
        run.close()


async def _do_download_album(io: common.ChatIO, status_msg, album_id: str, force: bool = False):
    # The run (when the status message carries one) gets what the cancel
    # report needs; a CancelledError itself propagates to _run_album.
    run: common.ActiveRun | None = getattr(status_msg, "run", None)
    async with state._download_semaphore:
        try:
            album = await metadata.fetch_album(album_id)
            await metadata.enrich_genres(album)
        except Exception as e:
            logger.error("Failed to fetch album %s: %s", album_id, e)
            await common.safe_edit(status_msg, f"❌ Failed to fetch album info: {common._short(e)}",
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
            existing_dir = await asyncio.to_thread(_locate_existing_album, config.MUSIC_DIR, album)
            if existing_dir and os.path.isdir(existing_dir):
                backup_root = os.path.join(config.MUSIC_DIR, ".redownload-backup")
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
        live = common.LiveStatus(
            edit_fn=lambda text: status_msg.edit_text(text, parse_mode="HTML"),
            render_fn=prog.render,
        )
        await live.refresh(force=True)

        async def on_event(ev: dict):
            await live.refresh(force=prog.handle(ev))

        try:
            result = await providers.download_album(
                album_id, config.MUSIC_DIR, album=album, on_event=on_event,
            )
        except Exception as e:
            logger.error("Album download failed (%s — %s): %s", album["artist"], album["title"], e)
            await common._put_back(run, backup_dir, restore_target)
            await common.safe_edit(status_msg, f"❌ Download failed: {common._short(e)}", final=True)
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
                await common._restore_backup(backup_dir, restore_target)
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
            await common.safe_edit(
                status_msg,
                f"Kept existing: {album['artist']} — {album['title']}\n"
                f"Re-download was incomplete ({got} ok, {miss} unavailable), "
                f"so the original copy was restored.",
                final=True,
            )
            return

        scan_note = ""
        if result["downloaded"] > 0:
            state._invalidate_retag_session("album downloaded")
            scan_note = await library_actions._trigger_scan()

        # Share link works even for "already in library" — useful when re-pasting old albums.
        share_url = await library_actions._try_share_album(
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

        await common._send_result(io, status_msg, done_text, result["album_dir"])


async def _download_track(update: Update, track_id: str, force: bool = False):
    io = common.ChatIO.from_update(update)
    if not await _prepare_download(io, "track", track_id, force):
        return
    common._spawn_run(_run_track(io, track_id, force=force), io, f"track {track_id}")


async def _run_track(io: common.ChatIO, track_id: str, force: bool = False,
                     resume_entry: journal.PendingDownload | None = None) -> bool:
    """Track twin of _run_album — same accept → journal → report → clear
    bracket. A pending lossy-fallback prompt counts as a reported outcome:
    prompts don't survive restarts by design."""
    key = f"track:{track_id}"
    run = common.ActiveRun(io.chat_id, "track")
    if key in state._in_flight:
        run.close()
        return False
    state._in_flight.add(key)
    run.attach()
    status_msg = None
    try:
        if state._download_semaphore.locked():
            status_msg = await common.safe_send(
                io, "Queued — will start after the current download finishes",
                reply_markup=run.markup)
        else:
            status_msg = await common.safe_send(io, "Fetching track info…",
                                         reply_markup=run.markup)
        status_msg = common.RunStatus(status_msg, run)
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
        await common._report_cancelled(status_msg, run, io)
        journal.remove("track", track_id, io.chat_id)
        return True
    finally:
        state._in_flight.discard(key)
        run.close()


async def _do_download_track(io: common.ChatIO, status_msg, track_id: str, force: bool = False):
    run: common.ActiveRun | None = getattr(status_msg, "run", None)
    async with state._download_semaphore:
        try:
            track, album_ctx = await metadata.fetch_single_track(track_id)
            if album_ctx:
                await metadata.enrich_genres(album_ctx)
        except Exception as e:
            logger.error("Failed to fetch track %s: %s", track_id, e)
            await common.safe_edit(status_msg, f"❌ Failed to fetch track info: {common._short(e)}",
                            final=True)
            return

        backup = existing = None
        if force:
            album_dir = _locate_existing_album(config.MUSIC_DIR, album_ctx)
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
        live = common.LiveStatus(
            edit_fn=lambda text: status_msg.edit_text(text, parse_mode="HTML"),
            render_fn=prog.render,
        )
        await live.refresh(force=True)

        async def on_event(ev: dict):
            await live.refresh(force=prog.handle(ev))

        try:
            path, was_downloaded, fmt = await providers.download_single_track(
                track, album_ctx, config.MUSIC_DIR, on_event=on_event,
            )
        except RuntimeError as e:
            if "No FLAC found" in str(e):
                outcome = await _offer_mp3_fallback(status_msg, track_id, track, album_ctx)
                if outcome == "prompt":
                    # The prompt owns the outcome now; the original is gone
                    # either way (force semantics), so drop the backup.
                    if run is not None:
                        run.backup = None
                    common._drop_backup(backup)
                    return
                await common._put_back(run, backup, existing)
                if outcome == "reported":
                    return
                await common.safe_edit(
                    status_msg,
                    f"❌ No FLAC or acceptable lossy fallback for "
                    f"{track['artist']} — {track['title']}",
                    final=True,
                )
                return
            await common._put_back(run, backup, existing)
            logger.error("Track download failed (%s — %s): %s", track["artist"], track["title"], e)
            await common.safe_edit(status_msg, f"❌ Download failed: {common._short(e)}", final=True)
            return
        except Exception as e:
            await common._put_back(run, backup, existing)
            logger.error("Track download failed (%s — %s): %s", track["artist"], track["title"], e)
            await common.safe_edit(status_msg, f"❌ Download failed: {common._short(e)}", final=True)
            return
        if run is not None:
            run.close()  # saved; scan and report are not cancellable
            run.backup = None
        common._drop_backup(backup)

        scan_note = ""
        if was_downloaded:
            state._invalidate_retag_session("track downloaded")
            scan_note = await library_actions._trigger_scan()

        share_url = await library_actions._try_share_album(
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

        await common._send_result(io, status_msg, done_text, os.path.dirname(path))


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
        candidates = await providers.find_lossy_candidates(track, album_ctx)
    except providers.SearchError as e:
        # Searches are being throttled or slskd is down — "no fallback found"
        # would be a lie. Report the real reason and stop here.
        logger.warning("Lossy fallback search couldn't run: %s", e)
        reason = "rate-limited" if isinstance(e, providers.SearchThrottledError) \
            else "search error"
        await common.safe_edit(
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
    old_run: common.ActiveRun | None = getattr(status_msg, "run", None)
    if old_run is not None:
        old_run.close()
    prompt_run = common.ActiveRun(status_msg.chat_id, "prompt")

    async def _cancel_prompt() -> None:
        _pending_lossy.pop(cid, None)
        prompt_run.close()
        await common.safe_edit(
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
    if user_id not in config.ALLOWED_USERS:
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
    common._active_runs.pop(entry.get("run_id", ""), None)  # the prompt's ✖ Cancel

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
    common._spawn_background(_run_lossy_download(query, entry))


async def _run_lossy_download(query, entry: dict) -> None:
    status = await providers.check()
    if not status.ready:
        await query.edit_message_text(
            f"{status.message}\nSend the link again after setup or recovery.",
            reply_markup=common._provider_actions())
        return
    track = entry["track"]
    album_ctx = entry["album_ctx"]
    candidates = entry["candidates"]
    chat_id = entry.get("chat_id")

    in_flight_key = f"track:{entry['track_id']}:lossy"
    if in_flight_key in state._in_flight:
        with contextlib.suppress(TelegramError):
            await query.edit_message_text("Already queued.")
        return
    state._in_flight.add(in_flight_key)
    run = common.ActiveRun(chat_id, "track")
    run.attach()
    status_msg = common.RunStatus(common._QueryMessage(query), run)
    try:
        summary = _format_lossy_summary(candidates)
        await common.safe_edit(
            status_msg, f"Downloading: {track['artist']} — {track['title']} ({summary})")
        async with state._download_semaphore:
            prog = reporting.TrackProgress(track["artist"], track["title"])
            run.saved_fn = lambda: prog.saved
            live = common.LiveStatus(
                edit_fn=lambda text: status_msg.edit_text(text, parse_mode="HTML"),
                render_fn=prog.render,
            )

            async def on_event(ev: dict):
                await live.refresh(force=prog.handle(ev))

            try:
                path, was_downloaded, fmt = await providers.download_single_track(
                    track, album_ctx, config.MUSIC_DIR,
                    accept_lossy=True,
                    precomputed_candidates=candidates,
                    on_event=on_event,
                )
            except Exception as e:
                logger.error(
                    "Lossy download failed (%s — %s): %s",
                    track["artist"], track["title"], e,
                )
                await common.safe_edit(status_msg, f"❌ Lossy download failed: {common._short(e)}",
                                final=True)
                return
            run.close()  # saved; scan and report are not cancellable

            if was_downloaded:
                state._invalidate_retag_session("lossy track downloaded")
            scan_note = await library_actions._trigger_scan()
            share_url = await library_actions._try_share_album(
                album_ctx.get("artist", track["artist"]),
                album_ctx.get("title", ""),
                skip_delay=False,
            )
            done_text = reporting.render_track_final(
                track["artist"], track["title"], fmt, prog.peer,
                time.monotonic() - prog.started,
                scan_note=scan_note, share_url=share_url,
            )
            await common.safe_edit(status_msg, done_text, final=True, parse_mode="HTML")
    except asyncio.CancelledError:
        if not run.cancel_requested:
            raise
        asyncio.current_task().uncancel()
        await common._report_cancelled(status_msg, run)
    except Exception:
        logger.exception("Lossy download task died (%s — %s)",
                         track["artist"], track["title"])
    finally:
        state._in_flight.discard(in_flight_key)
        run.close()


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
    status = await providers.check()
    if not status.ready:
        logger.info("Journal: preserving %d pending downloads; provider %s",
                    len(entries), status.state)
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
        io = common.ChatIO(app.bot, entry.chat_id)
        runner = _run_album if entry.kind == "album" else _run_track
        try:
            await common._await_in_own_task(runner(io, entry.id, resume_entry=entry))
        except Exception as e:
            # Entry stays in the journal (bumped) — next restart retries
            # until the cap; the failure itself is already logged/reported
            # by the flow where possible.
            logger.error("Resume of %s %s failed: %s", entry.kind, entry.id, e)
