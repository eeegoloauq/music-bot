import asyncio
import logging
import re
import time

import aiohttp

logger = logging.getLogger(__name__)

INSTANCES_URL = "https://monochrome.samidy.com/instances.json"
TIDAL_API_URL = "https://api.tidal.com/v1"
TIDAL_TOKEN = "CzET4vdadNUFQ5JU"
LRCLIB_URL = "https://lrclib.net/api/get"
ODESLI_URL = "https://api.song.link/v1-alpha.1/links"

_BUILTIN_INSTANCES = [
    "https://eu-central.monochrome.tf",
    "https://us-west.monochrome.tf",
    "https://arran.monochrome.tf",
    "https://api.monochrome.tf",
    "https://tidal-api.binimum.org",
    "https://monochrome-api.samidy.com",
    "https://triton.squid.wtf",
    "https://wolf.qqdl.site",
    "https://hifi-one.spotisaver.net",
    "https://hifi-two.spotisaver.net",
    "https://maus.qqdl.site",
    "https://vogel.qqdl.site",
    "https://hund.qqdl.site",
    "https://tidal.kinoplus.online",
    "https://katze.qqdl.site",
]

_instances: list[str] = []
_instances_updated: float = 0
_INSTANCES_TTL = 1800  # refresh every 30 min
_dead_instances: set[str] = set()  # connection-failed instances, cleared on refresh

_session: aiohttp.ClientSession | None = None
_lrclib_sem: asyncio.Semaphore | None = None  # created lazily in async context


def _get_lrclib_sem() -> asyncio.Semaphore:
    global _lrclib_sem
    if _lrclib_sem is None:
        _lrclib_sem = asyncio.Semaphore(10)
    return _lrclib_sem


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(trust_env=True)
    return _session


async def close():
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


async def _refresh_instances() -> list[str]:
    """Fetch instance list from remote, fall back to builtin."""
    global _instances, _instances_updated
    _dead_instances.clear()
    try:
        session = await _get_session()
        async with session.get(INSTANCES_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None)
        fetched = [url.rstrip("/") for url in data.get("api", [])]
        if fetched:
            _instances = fetched
            _instances_updated = time.monotonic()
            logger.info("Loaded %d instances from remote", len(_instances))
            return _instances
    except Exception as e:
        logger.warning("Failed to fetch instances: %s", e)
    if not _instances:
        _instances = list(_BUILTIN_INSTANCES)
        _instances_updated = time.monotonic()
        logger.info("Using %d builtin instances", len(_instances))
    return _instances


async def _get_instances() -> list[str]:
    if not _instances or (time.monotonic() - _instances_updated) > _INSTANCES_TTL:
        await _refresh_instances()
    return _instances


async def _api_get(path: str) -> dict:
    """GET from Monochrome API with instance failover. Unwraps {"data": ...}."""
    global _instances_updated
    instances = await _get_instances()
    session = await _get_session()
    last_err = None

    # Skip instances that failed with connection errors this session.
    # If all are dead, clear and try everyone (avoids permanent lockout).
    active = [i for i in instances if i not in _dead_instances]
    if not active:
        _dead_instances.clear()
        active = instances

    for inst in active:
        url = f"{inst}{path}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(connect=4, total=20)) as resp:
                if resp.status != 200:
                    last_err = f"HTTP {resp.status} from {inst}"
                    continue
                body = await resp.json(content_type=None)
                if isinstance(body, dict) and "detail" in body:
                    last_err = f"{inst}: {body['detail']}"
                    logger.warning("Instance %s: %s", inst, body["detail"])
                    continue
                if isinstance(body, dict) and "data" in body:
                    return body["data"]
                return body
        except Exception as e:
            last_err = f"{inst}: {e}"
            logger.warning("Instance %s failed: %s", inst, e)
            _dead_instances.add(inst)
            continue
    _instances_updated = 0  # force refresh on next call
    raise RuntimeError(f"All instances failed. Last error: {last_err}")


_SHAZAM_RE = re.compile(r"shazam\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?song/(\d+)")


def _shazam_to_apple(url: str) -> str:
    """Convert Shazam URL to Apple Music URL (Shazam song ID = Apple Music ID)."""
    m = _SHAZAM_RE.search(url)
    if m:
        return f"https://music.apple.com/us/song/{m.group(1)}"
    return url


async def resolve_link(url: str) -> tuple[str, str] | None:
    """Resolve a music platform URL to a Tidal ID via Odesli (song.link).

    Returns ("album", id) or ("track", id), or None if no Tidal match.
    """
    url = _shazam_to_apple(url)
    session = await _get_session()
    try:
        async with session.get(
            ODESLI_URL,
            params={"url": url},
            timeout=aiohttp.ClientTimeout(connect=5, total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("Odesli returned HTTP %d for %s", resp.status, url)
                return None
            data = await resp.json(content_type=None)
    except asyncio.TimeoutError:
        logger.warning("Odesli timed out for %s", url)
        return None
    except Exception as e:
        logger.warning("Odesli request failed: %s", e)
        return None

    tidal_link = (data.get("linksByPlatform") or {}).get("tidal", {}).get("url", "")
    if not tidal_link:
        return None

    m = re.search(r"/album/(\d+)", tidal_link)
    if m:
        return ("album", m.group(1))
    m = re.search(r"/track/(\d+)", tidal_link)
    if m:
        return ("track", m.group(1))

    logger.warning("Odesli returned unrecognized Tidal URL: %s", tidal_link)
    return None
