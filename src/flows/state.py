"""Shared download serialization and retag snapshot invalidation."""

import asyncio
import logging

logger = logging.getLogger(__name__)


# Serialize library writes across downloads, imports, retags and maintenance.
_download_semaphore = asyncio.Semaphore(1)


# Reject duplicate requests before they enter the serialized queue.
_in_flight: set[str] = set()


# One preview snapshot; owner/chat checks and fingerprints guard confirmation.
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
