"""Shared fixtures: a scripted fake slskd client and pacing-state reset.

The suite runs fully offline. ``soulseek.client`` talks to a fake
``searches`` API driven by a per-test script function, and every test
starts with the module-level pacing/throttle state zeroed so tests are
order-independent.
"""

import asyncio
import sys
import time
import types
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from soulseek import client as sc  # noqa: E402
from soulseek.client import SearchResult  # noqa: E402


@pytest.fixture(autouse=True)
def slskd_state(monkeypatch):
    """Zero the module-level pacing/throttle state before each test.

    Sets the search interval to 0 by default — tests that exercise pacing
    override it explicitly. monkeypatch restores the originals on teardown.
    """
    monkeypatch.setattr(sc, "_client", None)
    monkeypatch.setattr(sc, "_search_lock", asyncio.Lock())
    monkeypatch.setattr(sc, "_next_search_start", 0.0)
    monkeypatch.setattr(sc, "_zero_streak", 0)
    monkeypatch.setattr(sc, "_zero_verified_until", 0.0)
    monkeypatch.setattr(sc, "SEARCH_MIN_INTERVAL_SECS", 0.0)


def make_fake_searches(script):
    """Build a fake slskd client whose search API is driven by ``script``.

    ``script(call_index)`` returns the raw peer-response list for the i-th
    started search, or raises (e.g. ``requests.HTTPError``) like the real
    API would. Returns ``(fake_client, calls)`` where ``calls`` records
    ``n`` (start attempts, including ones that raised) and ``starts``
    (``(monotonic_time, query)`` per attempt).
    """
    calls = {"n": 0, "starts": []}

    class FakeSearches:
        def search_text(self, searchText, searchTimeout, responseLimit):
            i = calls["n"]
            calls["n"] += 1
            calls["starts"].append((time.monotonic(), searchText))
            result = script(i)  # may raise
            return {"id": f"s{i}", "_responses": result}

        def state(self, id):
            return {"id": id, "isComplete": True, "fileCount": 0}

        def stop(self, id):
            pass

        def search_responses(self, id):
            return script(int(id[1:]))

        def delete(self, id):
            pass

    return types.SimpleNamespace(searches=FakeSearches()), calls


def install_fake_client(monkeypatch, script):
    """Wire a scripted fake client into ``soulseek.client``; returns ``calls``."""
    fake, calls = make_fake_searches(script)
    monkeypatch.setattr(sc, "_client", fake)
    return calls


def http_error(status: int) -> requests.HTTPError:
    resp = types.SimpleNamespace(status_code=status)
    return requests.HTTPError(f"{status} error", response=resp)


# A single healthy peer offering one well-named FLAC.
PEER_RESPONSE = [{
    "username": "gooduser",
    "hasFreeUploadSlot": True,
    "uploadSpeed": 2_000_000,
    "queueLength": 0,
    "files": [{"filename": "Music\\Artist - Album\\01 - Song.flac",
               "size": 30_000_000, "length": 200, "bitDepth": 16,
               "sampleRate": 44100}],
}]


def make_result(username: str, filename: str, *, length: int = 200,
                has_free_slot: bool = True, upload_speed: int = 2_000_000,
                queue_length: int = 0, bit_depth: int | None = 16,
                sample_rate: int | None = 44100,
                bit_rate: int | None = None,
                size: int = 30_000_000) -> SearchResult:
    return SearchResult(
        username=username, filename=filename, size=size, bit_rate=bit_rate,
        bit_depth=bit_depth, sample_rate=sample_rate, length=length,
        has_free_slot=has_free_slot, upload_speed=upload_speed,
        queue_length=queue_length,
    )
