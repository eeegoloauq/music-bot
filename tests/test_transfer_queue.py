"""Queue visibility while slskd waits on a remote peer."""

import asyncio
import types

from soulseek import client as sc


def _row(state="Queued, Remotely", *, speed=0, pct=0):
    return {
        "id": "transfer-guid",
        "filename": "Album\\01 - Song.flac",
        "state": state,
        "percentComplete": pct,
        "averageSpeed": speed,
        "bytesTransferred": 0,
        "size": 100,
    }


def _install_fake_clock(monkeypatch):
    clock = types.SimpleNamespace(now=0.0)
    clock.monotonic = lambda: clock.now
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        clock.now += delay
        await real_sleep(0)

    monkeypatch.setattr(sc, "time", clock)
    monkeypatch.setattr(sc.asyncio, "sleep", fake_sleep)
    return clock, real_sleep


async def test_remote_queue_heartbeats_and_transfer_transition(monkeypatch):
    _install_fake_clock(monkeypatch)
    rows = iter([
        [_row()],
        [_row()],
        [_row()],
        [_row("InProgress", speed=20, pct=0)],
        [_row("Completed, Succeeded", speed=20, pct=100)],
    ])

    async def downloads(_username):
        return next(rows)

    async def position(_username, _transfer_id):
        return 7

    monkeypatch.setattr(sc, "get_downloads", downloads)
    monkeypatch.setattr(sc, "_get_queue_position", position)
    monkeypatch.setattr(sc, "_QUEUE_HEARTBEAT_SECS", 2.0)
    state_updates = []
    progress_updates = []

    async def on_state(*args):
        state_updates.append(args)

    async def on_progress(*args):
        progress_updates.append(args)

    states = await sc.wait_for_files(
        "peer", ["Album\\01 - Song.flac"], timeout_secs=20,
        poll_interval=1, progress_cb=on_progress, state_cb=on_state,
    )

    queued = [update for update in state_updates if "Remotely" in update[1]]
    assert len(queued) >= 2
    assert any(update[2] == 7 for update in queued)
    assert state_updates[-1][1] == "Completed, Succeeded"
    assert state_updates[-1][2:] == (None, 0.0)
    assert len(progress_updates) == 3  # same pct still reports state/speed changes
    assert states["Album\\01 - Song.flac"] == "Completed, Succeeded"


async def test_queue_position_error_is_cosmetic_and_throttled(monkeypatch):
    _install_fake_clock(monkeypatch)
    monkeypatch.setattr(sc, "get_downloads", lambda _username: _async([_row()]))
    calls = 0

    async def failed_position(_username, _transfer_id):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(sc, "_get_queue_position", failed_position)
    monkeypatch.setattr(sc, "_QUEUE_POSITION_INTERVAL_SECS", 10.0)
    updates = []

    async def on_state(*args):
        updates.append(args)

    states = await sc.wait_for_files(
        "peer", ["Album\\01 - Song.flac"], timeout_secs=3,
        poll_interval=1, state_cb=on_state,
    )

    assert calls == 1
    assert updates[0][2] is None
    assert states["Album\\01 - Song.flac"] == \
        "TimedOut, ClientSide, QueuedRemotely"


async def test_queue_position_error_string_is_unknown(monkeypatch):
    async def fake_to_thread(_func, **_kwargs):
        return "HTTP 500 from peer"

    monkeypatch.setattr(sc, "_client", types.SimpleNamespace(
        transfers=types.SimpleNamespace(get_queue_position=lambda **_kwargs: 1)))
    monkeypatch.setattr(sc.asyncio, "to_thread", fake_to_thread)
    assert await sc._get_queue_position("peer", "transfer-guid") is None


async def test_pending_queue_lookup_is_not_replaced_or_abandoned(monkeypatch):
    _clock, real_sleep = _install_fake_clock(monkeypatch)
    rows = iter([
        [_row()],
        [_row()],
        [_row()],
        [_row("Completed, Succeeded", pct=100)],
    ])

    async def downloads(_username):
        return next(rows)

    monkeypatch.setattr(sc, "get_downloads", downloads)
    monkeypatch.setattr(sc, "_QUEUE_POSITION_INTERVAL_SECS", 1.0)
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending_position(_username, _transfer_id):
        nonlocal calls
        calls += 1
        started.set()
        try:
            await release.wait()
            return 4
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(sc, "_get_queue_position", pending_position)
    await sc.wait_for_files(
        "peer", ["Album\\01 - Song.flac"], timeout_secs=20,
        poll_interval=1,
    )
    assert started.is_set()
    assert calls == 1
    assert not cancelled.is_set()
    release.set()
    await real_sleep(0)
    assert not cancelled.is_set()


async def _async(value):
    return value
