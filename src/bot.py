"""Application composition and process lifecycle."""

import contextlib
import errno
import logging
import os
import time

from telegram import BotCommand, Update
from telegram.error import NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    InlineQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest

from flows import common, downloads, handlers
from flows import uploads as upload_flow
from inline import handle_inline_query
import config
import journal
import metadata
import navidrome
import providers
import upload_web
import uploads

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


_RESTART_DELAY = 30  # seconds between automatic restarts after a crash


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
    commands = [
        BotCommand("help", "Show all features"),
        BotCommand("scan", "Trigger Navidrome library rescan"),
        BotCommand("upload", "Import your music"),
        BotCommand("provider", "Download provider status and setup"),
        BotCommand("stats", "Library statistics"),
        BotCommand("library", "Browse albums and preview tag changes"),
        BotCommand("maintain", "Choose library fields to update"),
        BotCommand("import", "Select an upload release manually"),
        BotCommand("retag", "Re-tag library from current Deezer + Last.fm metadata"),
    ]
    if providers.sharing_enabled():
        commands.append(BotCommand("sharescan", "Trigger Soulseek share rescan"))
    await app.bot.set_my_commands(commands)
    # Reclaim space from prior failed sessions — partial files left in
    # the slskd staging dir would otherwise pile up forever. Async so the
    # bot is responsive even on a slow fs scan.
    status = await providers.check()
    logger.info("Download provider: %s", status.state)
    if status.ready:
        common._spawn_background(providers.cleanup_orphan_staging())
        # Pick up downloads a restart orphaned. Unavailable providers leave
        # the journal untouched, including its resume-attempt counters.
        common._spawn_background(downloads._resume_pending(app))
    # Local-upload intake: watch /data/uploads for dropped zips/folders,
    # then identify/tag/file each one like a normal download, reporting to
    # the owner (first ALLOWED_USERS id).
    if config.ALLOWED_USERS:
        owner_io = common.ChatIO(app.bot, config.ALLOWED_USERS[0])

        async def _on_upload(report: uploads.IntakeReport) -> None:
            with contextlib.suppress(TelegramError):
                await upload_flow._upload_choices.offer(owner_io, report)

        async def _watch_uploads() -> None:
            # Re-offer unfinished uploads before intake starts to avoid duplicate prompts.
            await upload_flow._upload_choices.restore(owner_io)
            await uploads.watch_loop(_on_upload)

        common._spawn_background(_watch_uploads())
    # One-page upload site feeding the same watched folder (off unless
    # UPLOAD_HTTP_PORT is set).
    if config.UPLOAD_HTTP_PORT:
        await upload_web.start(config.UPLOAD_HTTP_PORT)


async def _shutdown(app: Application) -> None:
    await metadata.close()
    await providers.close()
    await navidrome.close()
    await upload_web.stop()


def _build_app() -> Application:
    app = (
        Application.builder()
        .token(config.TG_TOKEN)
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
    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("help", handlers.cmd_help))
    app.add_handler(CommandHandler("provider", handlers.cmd_provider))
    app.add_handler(CommandHandler("upload", handlers.cmd_upload))
    app.add_handler(CommandHandler("scan", handlers.cmd_scan))
    app.add_handler(CommandHandler("sharescan", handlers.cmd_share_scan))
    app.add_handler(CommandHandler("stats", handlers.cmd_stats))
    app.add_handler(CommandHandler("retag", handlers.cmd_retag))
    app.add_handler(CommandHandler("library", handlers.cmd_library))
    app.add_handler(CommandHandler("maintain", handlers.cmd_maintain))
    app.add_handler(CallbackQueryHandler(handlers._handle_maintenance_callback,
                                        pattern=r"^maintain:(titles|genres|lyrics|artwork|preview|close):[a-f0-9]{12}$"))
    app.add_handler(CallbackQueryHandler(handlers._handle_album_callback, pattern=r"^album:(open|preview|maintain):"))
    app.add_handler(CallbackQueryHandler(handlers._handle_retag_callback, pattern=r"^retag:(confirm|stop):"))
    app.add_handler(CallbackQueryHandler(upload_flow._upload_choices.list_pending, pattern=r"^uploads:pending$"))
    app.add_handler(CommandHandler("import", upload_flow._upload_choices.manual))
    app.add_handler(CallbackQueryHandler(upload_flow._upload_choices.callback, pattern=r"^import:(original|match):[a-f0-9]{12}$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message))
    app.add_handler(InlineQueryHandler(handle_inline_query))
    app.add_handler(CallbackQueryHandler(handlers._handle_lossy_callback, pattern=r"^lossy:"))
    app.add_handler(CallbackQueryHandler(handlers._handle_import_help, pattern=r"^import:help$"))
    app.add_handler(CallbackQueryHandler(handlers._handle_cancel_callback, pattern=r"^cancel:"))
    app.add_handler(CallbackQueryHandler(handlers._handle_delete_callback, pattern=r"^rm:(yes|no):"))
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
                        ("library", config.MUSIC_DIR)):
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

    if not config.NAVI_LOGIN or not config.NAVI_PASS:
        logger.warning(
            "NAVIDROME_USER/NAVIDROME_PASS not set — scan, inline audio, and share will not work"
        )
    if not config.NAVI_PUBLIC_URL:
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
