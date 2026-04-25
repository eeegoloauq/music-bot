"""Shared aiohttp session, lrclib helpers, and constants for metadata module."""

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

LRCLIB_URL = "https://lrclib.net/api/get"
ODESLI_URL = "https://api.song.link/v1-alpha.1/links"
DEEZER_API = "https://api.deezer.com"

_session: aiohttp.ClientSession | None = None
_lrclib_sem: asyncio.Semaphore | None = None


def _get_lrclib_sem() -> asyncio.Semaphore:
    global _lrclib_sem
    if _lrclib_sem is None:
        _lrclib_sem = asyncio.Semaphore(10)
    return _lrclib_sem


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        # trust_env=True so HTTP_PROXY / HTTPS_PROXY / NO_PROXY are respected
        # (aiohttp's default is False — env vars get ignored without this).
        # Operators whose proxy gets throttled by a specific upstream
        # (Odesli, Deezer, lrclib) add that host to NO_PROXY to send those
        # requests direct while keeping the proxy for everything else.
        _session = aiohttp.ClientSession(trust_env=True)
    return _session


async def close():
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


def cover_url(stored: str, size: int = 1000) -> str:
    """Resize a Deezer cover URL (``…/{N}x{N}-000000-80-0-0.jpg``) to the
    requested square pixel size. Empty input → empty output.
    """
    import re
    if not stored:
        return ""
    return re.sub(
        r"/\d+x\d+(?:[-\d]+)?\.jpg",
        f"/{size}x{size}-000000-80-0-0.jpg",
        stored,
    )
