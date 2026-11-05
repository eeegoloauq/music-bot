"""Configuration, health and lazy access to optional audio providers."""

from dataclasses import dataclass
import importlib
import importlib.util
import sys

import aiohttp

from config import (
    DOWNLOAD_PROVIDER, SLSKD_HOST, SLSKD_API_KEY, SOULSEEK_SHARE_SCAN_ENABLED,
)


class SearchError(Exception):
    """A provider search could not run; an empty result means it did run."""


class SearchThrottledError(SearchError):
    """The provider is rate-limiting searches."""


@dataclass(frozen=True)
class ProviderStatus:
    state: str
    message: str

    @property
    def ready(self) -> bool:
        return self.state == "ready"


def configured() -> bool:
    return DOWNLOAD_PROVIDER == "soulseek" and bool(SLSKD_HOST)


def sharing_enabled() -> bool:
    return (configured() and SOULSEEK_SHARE_SCAN_ENABLED
            and importlib.util.find_spec("slskd_api") is not None)


async def check() -> ProviderStatus:
    if DOWNLOAD_PROVIDER == "none":
        return ProviderStatus("disabled", "No download provider is configured.")
    if not SLSKD_HOST:
        return ProviderStatus("misconfigured", "Download provider setup is incomplete.")
    if importlib.util.find_spec("slskd_api") is None:
        return ProviderStatus("misconfigured", "The Soulseek provider package is not installed.")
    try:
        async with aiohttp.ClientSession(
            trust_env=True, timeout=aiohttp.ClientTimeout(total=5),
        ) as session:
            async with session.get(
                f"{SLSKD_HOST}/api/v0/server",
                headers={"X-API-Key": SLSKD_API_KEY or "anonymous"},
            ) as response:
                response.raise_for_status()
                state = await response.json()
        if not isinstance(state, dict) or state.get("isLoggedIn") is not True:
            return ProviderStatus("unavailable", "The download provider is not connected.")
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return ProviderStatus("unavailable", "The download provider is unavailable.")
    return ProviderStatus("ready", "Soulseek is ready.")


def _backend():
    if not configured():
        raise SearchError("No download provider is configured")
    return importlib.import_module("soulseek")


async def download_album(*args, **kwargs):
    return await _backend().download_album(*args, **kwargs)


async def download_single_track(*args, **kwargs):
    return await _backend().download_single_track(*args, **kwargs)


async def find_lossy_candidates(*args, **kwargs):
    return await _backend().find_lossy_candidates(*args, **kwargs)


async def cleanup_orphan_staging():
    if configured():
        await _backend().cleanup_orphan_staging()


async def rescan_shares():
    if sharing_enabled():
        return await _backend().rescan_shares()
    return False


def schedule_rescan_shares():
    if sharing_enabled():
        return _backend().schedule_rescan_shares()
    return 0


async def close():
    # Importing the optional package during shutdown would break base installs.
    if "soulseek" in sys.modules:
        await sys.modules["soulseek"].close()
