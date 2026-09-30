"""Stall guard: a started transfer that stops moving bytes is given up on."""

import asyncio
import types

import pytest

from soulseek import client as sc
from soulseek import downloader
from soulseek.downloader import PeerTransferError

FNAME = "Album\\01 - Song.flac"


def _row(state, got=0):
    return {"id": "transfer-guid", "filename": FNAME, "state": state,
            "percentComplete": got, "averageSpeed": 0,
            "bytesTransferred": got, "size": 100}


def _install_fake_clock(monkeypatch):
    clock = types.SimpleNamespace(now=0.0)
    clock.monotonic = lambda: clock.now
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        clock.now += delay
        await real_sleep(0)

    monkeypatch.setattr(sc, "time", clock)
    monkeypatch.setattr(sc.asyncio, "sleep", fake_sleep)
    return clock


def _feed(monkeypatch, rows):
    """slskd returns ``rows`` one poll at a time, then repeats the last one."""
    rows = list(rows)

    async def downloads(_username):
        return [rows.pop(0) if len(rows) > 1 else rows[0]]

    monkeypatch.setattr(sc, "get_downloads", downloads)


async def test_started_transfer_without_bytes_is_given_up(monkeypatch):
    clock = _install_fake_clock(monkeypatch)
    _feed(monkeypatch, [_row("InProgress", 10)])
    states = await sc.wait_for_files("peer", [FNAME], timeout_secs=900,
                                     poll_interval=2.0, stall_secs=30)
    assert states == {FNAME: sc.STALLED_STATE}
    assert 30 <= clock.now < 40  # well before the 15-minute attempt cap


async def test_slow_but_moving_transfer_is_left_alone(monkeypatch):
    _install_fake_clock(monkeypatch)
    # One byte per poll for the whole stall window and beyond, then done.
    moving = [_row("InProgress", i) for i in range(1, 60)]
    _feed(monkeypatch, moving + [_row("Completed, Succeeded", 100)])
    states = await sc.wait_for_files("peer", [FNAME], timeout_secs=900,
                                     poll_interval=2.0, stall_secs=30)
    assert states == {FNAME: "Completed, Succeeded"}


async def test_queue_time_does_not_count_as_a_stall(monkeypatch):
    _install_fake_clock(monkeypatch)
    queued = [_row("Queued, Remotely")] * 100  # 200 s in the peer's queue

    async def position(_username, _transfer_id):
        return 3

    monkeypatch.setattr(sc, "_get_queue_position", position)
    _feed(monkeypatch, queued + [_row("InProgress", 50), _row("Completed, Succeeded", 100)])
    states = await sc.wait_for_files("peer", [FNAME], timeout_secs=900,
                                     poll_interval=2.0, stall_secs=30)
    assert states == {FNAME: "Completed, Succeeded"}


async def test_zero_disables_the_guard(monkeypatch):
    clock = _install_fake_clock(monkeypatch)
    _feed(monkeypatch, [_row("InProgress", 10)])
    states = await sc.wait_for_files("peer", [FNAME], timeout_secs=120,
                                     poll_interval=2.0, stall_secs=0)
    assert states[FNAME].startswith("TimedOut")  # ran into the attempt cap instead
    assert clock.now >= 120


async def test_stalled_attempt_is_cancelled_and_retried_elsewhere(monkeypatch):
    """Downloader side: a stalled result is an ordinary per-peer failure —
    slskd drops the transfer, staging is cleaned, the caller tries the next peer."""
    cancelled, removed = [], []

    async def fake_enqueue(_chosen):
        pass

    async def fake_await(username, filename, on_progress=None, on_state=None):
        return sc.STALLED_STATE

    async def fake_cancel(username, filename, remove=False):
        cancelled.append((username, filename))

    monkeypatch.setattr(downloader, "_ensure_enqueued", fake_enqueue)
    monkeypatch.setattr(downloader, "_await_one_file", fake_await)
    monkeypatch.setattr(downloader.slskd, "cancel_download", fake_cancel)
    monkeypatch.setattr(downloader, "_remove_staging_traces",
                        lambda u, f: removed.append((u, f)))

    chosen = types.SimpleNamespace(username="peer", filename="a\\b.flac", extension="flac")
    with pytest.raises(PeerTransferError, match="Stalled"):
        await downloader._download_chosen(
            chosen, {"title": "b", "artist": "x"}, {}, "/nonexistent", None, None)
    assert cancelled == [("peer", "a\\b.flac")]
    assert removed == [("peer", "a\\b.flac")]
