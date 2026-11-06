"""Drive staged uploads through identification, filing, and reporting."""

import asyncio
import logging
import os

from flows import state, common, library_actions
from upload_choices import UploadChoices
import config
import metadata
import reporting
import upload_import
import uploads

logger = logging.getLogger(__name__)


async def _handle_upload(io: common.ChatIO, report: uploads.IntakeReport,
                         mode: str = "match", album_id: str | None = None) -> None:
    """Drive a staged local upload through the same metadata → tag → file →
    scan → report path a Soulseek download takes (docs/local-upload-plan.md)."""
    if report.error:
        await io.reply_text(uploads.format_rejection(report))
        return

    run = common.ActiveRun(io.chat_id, "upload")
    run.attach()
    status_msg = None
    try:
        status_msg = common.RunStatus(await common.safe_send(
            io, f"📦 {report.name}: {len(report.audio)} audio file(s) received. " +
            ("Keeping original files…" if mode == "original" else "Identifying release…"),
            reply_markup=run.markup), run)
        await _import_upload(io, status_msg, report, run, mode=mode, album_id=album_id)
    except asyncio.CancelledError:
        if not run.cancel_requested:
            raise
        # Staged files are the user's own — a cancel leaves them where a
        # failed import would: in uploads staging, nothing deleted.
        asyncio.current_task().uncancel()
        if run.saved_fn and run.saved_fn():
            state._invalidate_retag_session("upload partially imported")
            await library_actions._trigger_scan()
        await common._report_cancelled(status_msg, run, io)
    finally:
        run.close()


async def _import_upload(io: common.ChatIO, status_msg, report: uploads.IntakeReport,
                         run: common.ActiveRun, mode: str = "match",
                         album_id: str | None = None) -> None:
    diag: dict = {}
    try:
        if mode != "original" and not album_id:
            album_id = await upload_import.identify_album(
                report.staging_dir, report.name, diag=diag)
    except Exception as e:
        logger.error("Upload identify failed for %s: %s", report.name, e)
    if mode != "original" and not album_id:
        await common.safe_edit(
            status_msg,
            f"❓ {report.name}: couldn't match this to a release — "
            f"{upload_import.describe_identify_failure(diag)}. Files are "
            "kept for you. Choose how to import them below.", final=True)
        await _upload_choices.offer(io, report)
        return

    if state._download_semaphore.locked():
        logger.info("Upload release identified for %s; import queued until the "
                    "current download finishes", report.name)
        await common.safe_edit(
            status_msg,
            f"📦 {report.name}: release identified. Import queued until the "
            "current download finishes…")

    progress: dict = {"saved": 0}
    run.saved_fn = lambda: progress["saved"]
    async with state._download_semaphore:
        try:
            if mode == "original":
                run.total = len(report.audio)
                album, result = await upload_import.import_original(report, config.MUSIC_DIR, progress)
            else:
                album = await metadata.fetch_album(album_id)
                await metadata.enrich_genres(album)
                run.total = len(album["tracks"])
                # Cosmetic edit inside the critical path — safe_edit, never a
                # bare edit_text: a network blip here once aborted the whole
                # import before a single file was filed.
                await common.safe_edit(
                    status_msg,
                    f"📦 Importing upload: {album['artist']} — {album['title']}…")
                result = await upload_import.import_staged_album(
                    album, report.staging_dir, config.MUSIC_DIR, progress=progress)
        except Exception as e:
            logger.exception("Upload import failed for %s", report.name)
            if progress["saved"]:
                state._invalidate_retag_session("upload partially imported")
                await library_actions._trigger_scan()
            await common.safe_edit(
                status_msg,
                f"❌ Upload import failed: {common._short(e)}. Files kept in staging.",
                final=True)
            return
    run.close()  # filed; scan and report are not cancellable

    scan_note = ""
    if result["downloaded"]:
        state._invalidate_retag_session("upload imported")
        scan_note = await library_actions._trigger_scan()
    share_url = None

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
    await common._send_result(io, status_msg, done_text, result["album_dir"])


_upload_choices = UploadChoices(_handle_upload, common.ChatIO)
