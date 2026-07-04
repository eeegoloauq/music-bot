"""Search pacing and throttle handling in ``soulseek.client``.

The Soulseek server silently drops search floods, so the client serializes
searches, paces their starts, backs off on 429, and treats bursts of
all-empty results as suspected throttling. A search that couldn't run must
raise — ``[]`` always means "ran, genuinely nothing".
"""

import asyncio
import time

import pytest

from soulseek import client as sc
from soulseek.client import SearchError, SearchThrottledError

from conftest import PEER_RESPONSE, http_error, install_fake_client


async def test_searches_serialized_and_paced(monkeypatch):
    calls = install_fake_client(monkeypatch, lambda i: PEER_RESPONSE)
    monkeypatch.setattr(sc, "SEARCH_MIN_INTERVAL_SECS", 0.5)

    await asyncio.gather(sc.search("q1"), sc.search("q2"), sc.search("q3"))

    starts = [t for t, _ in calls["starts"]]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert calls["n"] == 3
    assert all(g >= 0.45 for g in gaps), gaps


async def test_429_backs_off_and_retries_to_success(monkeypatch):
    def script(i):
        if i < 2:
            raise http_error(429)
        return PEER_RESPONSE

    calls = install_fake_client(monkeypatch, script)
    monkeypatch.setattr(sc, "_HTTP_429_BACKOFFS_SECS", (0.2, 0.2, 0.2))

    t0 = time.monotonic()
    responses = await sc.search("q")
    elapsed = time.monotonic() - t0

    assert len(responses) == 1
    assert calls["n"] == 3
    assert elapsed >= 0.4  # both backoff waits actually happened


async def test_persistent_429_raises_throttled_never_empty(monkeypatch):
    def script(i):
        raise http_error(429)

    calls = install_fake_client(monkeypatch, script)
    monkeypatch.setattr(sc, "_HTTP_429_BACKOFFS_SECS", (0.05, 0.05))

    with pytest.raises(SearchThrottledError):
        await sc.search("q")
    assert calls["n"] == 3  # attempts = backoffs + 1


async def test_zero_burst_cooldown_probe_recovers(monkeypatch):
    # Calls 0..2 come back empty (the suspicious burst); call 3 is the
    # post-cooldown probe of the same query — this time peers answer.
    def script(i):
        return [] if i < 3 else PEER_RESPONSE

    calls = install_fake_client(monkeypatch, script)
    monkeypatch.setattr(sc, "_ZERO_BURST_COOLDOWN_SECS", 0.3)

    r1 = await sc.search("a")
    r2 = await sc.search("b")
    t0 = time.monotonic()
    r3 = await sc.search("c")
    elapsed = time.monotonic() - t0

    assert r1 == [] and r2 == []
    assert len(r3) == 1  # recovered via the probe
    assert calls["starts"][3][1] == "c"  # probe re-ran the same query
    assert elapsed >= 0.3  # cooldown was honoured
    assert sc._zero_streak == 0  # streak reset after the hit


async def test_zero_burst_probe_confirms_emptiness(monkeypatch):
    calls = install_fake_client(monkeypatch, lambda i: [])
    monkeypatch.setattr(sc, "_ZERO_BURST_COOLDOWN_SECS", 0.2)

    for q in ("a", "b", "c"):
        await sc.search(q)
    assert calls["n"] == 4  # exactly one probe fired

    # Probe came back empty too — trust further empties, no repeat cooldowns.
    await sc.search("d")
    await sc.search("e")
    assert calls["n"] == 6
    assert sc._zero_verified_until > time.monotonic()


async def test_non_429_api_error_raises_search_error(monkeypatch):
    def script(i):
        raise http_error(500)

    install_fake_client(monkeypatch, script)

    with pytest.raises(SearchError) as excinfo:
        await sc.search("q")
    assert not isinstance(excinfo.value, SearchThrottledError)
