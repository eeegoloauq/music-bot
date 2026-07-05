"""Crash ledger for accepted download requests.

One JSON file holding every download request whose terminal outcome the user
has not seen yet. Written when a request is accepted, removed when the bot
reports success *or* failure — either way the user saw an answer. On
startup the bot re-issues whatever is left (see bot._resume_pending): the
library's tag-based dedup makes re-running an album idempotent, so the
ledger persists *intent*, never transfer state (slskd owns that).

Everything here fails open: a journal that can't be read or written must
never block a download — persistence is best-effort, the download is the
job. IO errors log a warning and return; corrupt content is discarded
wholesale.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

JOURNAL_PATH = os.environ.get("JOURNAL_PATH", "/data/pending-downloads.json")

# A request that keeps killing the bot must not become a crash loop.
MAX_RESUME_ATTEMPTS = 2


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class PendingDownload:
    kind: str                          # "album" | "track"
    id: str                            # Deezer id
    chat_id: int
    status_message_id: int | None = None
    force: bool = False
    requested_at: str = field(default_factory=_utcnow)
    resume_attempts: int = 0

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.kind, self.id, self.chat_id)


def _write(entries: list[PendingDownload]) -> None:
    """Atomic full-file rewrite. Files land 0o666 like everything else the
    bot writes to shared mounts, so the host user can manage them."""
    tmp = JOURNAL_PATH + ".tmp"
    os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump([asdict(e) for e in entries], f, indent=1)
    os.replace(tmp, JOURNAL_PATH)
    try:
        os.chmod(JOURNAL_PATH, 0o666)
    except OSError:
        pass


def load() -> list[PendingDownload]:
    """Read the ledger; missing or corrupt content yields [] (fail open)."""
    try:
        with open(JOURNAL_PATH) as f:
            raw = json.load(f)
        return [PendingDownload(**e) for e in raw]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError) as e:
        logger.warning("Discarding unreadable journal %s: %s", JOURNAL_PATH, e)
        return []


def add(entry: PendingDownload) -> None:
    """Record an accepted request. Replaces an existing entry with the same
    (kind, id, chat_id) — a re-request supersedes, never duplicates."""
    try:
        entries = [e for e in load() if e.key != entry.key]
        entries.append(entry)
        _write(entries)
    except OSError as e:
        logger.warning("Journal add failed (continuing without): %s", e)


def remove(kind: str, id: str, chat_id: int) -> None:
    """Drop a request once its outcome was reported to the user."""
    try:
        entries = load()
        remaining = [e for e in entries if e.key != (kind, id, chat_id)]
        if len(remaining) != len(entries):
            _write(remaining)
    except OSError as e:
        logger.warning("Journal remove failed: %s", e)


def bump_attempts(entry: PendingDownload) -> None:
    """Persist an incremented resume counter *before* the resume runs, so a
    crash during the resume itself still counts against the cap."""
    entry.resume_attempts += 1
    try:
        entries = [e for e in load() if e.key != entry.key]
        entries.append(entry)
        _write(entries)
    except OSError as e:
        logger.warning("Journal bump failed: %s", e)
