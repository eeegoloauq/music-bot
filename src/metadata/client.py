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
        # trust_env=False intentionally: the bot's HTTP_PROXY/HTTPS_PROXY env
        # vars route through a shared SOCKS proxy that Odesli/Deezer throttle
        # by source IP, kicking us into 429 even at low request rates. We hit
        # these public APIs directly (they're public, no proxy needed) so
        # latency is better and we don't share a rate-limit pool with other
        # users of the same proxy.
        _session = aiohttp.ClientSession(trust_env=False)
    return _session


async def close():
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


def cover_url(stored: str, size: int = 1000) -> str:
    """Resize a Deezer cover URL to the requested square pixel size.

    Deezer cover URLs follow the pattern
    ``https://cdn-images.dzcdn.net/images/cover/{md5}/{N}x{N}-000000-80-0-0.jpg``.
    For passthrough — anything that starts with http is assumed already a usable
    URL and we just substitute the size segment when it looks like the Deezer
    pattern. Empty input → empty output.
    """
    import re
    if not stored:
        return ""
    if not stored.startswith(("http://", "https://")):
        # Backwards compat: legacy Tidal-CDN UUID stored without scheme
        return f"https://resources.tidal.com/images/{stored.replace('-', '/')}/{size}x{size}.jpg"
    return re.sub(
        r"/\d+x\d+(?:[-\d]+)?\.jpg",
        f"/{size}x{size}-000000-80-0-0.jpg",
        stored,
    )
