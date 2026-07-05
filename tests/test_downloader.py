"""Peer-retry bookkeeping in ``soulseek.downloader``: failed peers are
remembered across an album's phases and never re-attempted — and enqueueing
converges on transfers that are already live in slskd (resume path)."""

import types

import pytest

from soulseek import client as sc
from soulseek import downloader
from soulseek.downloader import PeerTransferError

from conftest import make_result


async def test_failed_peers_skipped_across_phases(monkeypatch):
    attempts = []

    async def fake_download(chosen, *args, **kwargs):
        attempts.append(chosen.username)
        if chosen.username == "badpeer":
            raise PeerTransferError("transfer ended in state 'Completed, Errored'")
        return ("/x.flac", 1, "FLAC")

    monkeypatch.setattr(downloader, "_download_chosen", fake_download)

    failed: set[tuple[str, str]] = set()
    bad = make_result("badpeer", "d\\01.flac")
    good = make_result("okpeer", "d\\01.flac")

    res = await downloader._try_candidates(
        [bad, good], {"title": "t"}, {}, "/tmp", None, None, failed_keys=failed)
    assert res is not None
    assert res[3].username == "okpeer"  # fell through to the second peer
    assert ("badpeer", "d\\01.flac") in failed

    # A later phase offering only the known-bad peer must raise, not re-wait
    # on a peer that already proved broken this album.
    attempts.clear()
    with pytest.raises(PeerTransferError, match="already failed"):
        await downloader._try_candidates(
            [bad], {"title": "t"}, {}, "/tmp", None, None, failed_keys=failed)
    assert attempts == []  # known-bad peer was not re-attempted


# --- convergent enqueue (resume path) ---------------------------------------

def _wire_transfers(monkeypatch, active_states, enqueue_ok=True):
    """Fake the two slskd calls _ensure_enqueued makes. ``active_states`` is
    consumed one value per get_active_download_state call."""
    calls = {"enqueue": 0, "checks": 0}
    states = list(active_states)

    async def fake_active(username, filename):
        calls["checks"] += 1
        return states.pop(0) if states else None

    async def fake_enqueue(username, files):
        calls["enqueue"] += 1
        return enqueue_ok

    monkeypatch.setattr(downloader.slskd, "get_active_download_state", fake_active)
    monkeypatch.setattr(downloader.slskd, "enqueue", fake_enqueue)
    return calls


CHOSEN = make_result("peer", "M\\A\\01 - Song.flac")


async def test_attaches_to_live_transfer_without_enqueueing(monkeypatch):
    calls = _wire_transfers(monkeypatch, active_states=["InProgress"])
    await downloader._ensure_enqueued(CHOSEN)
    assert calls["enqueue"] == 0  # never re-enqueues an active transfer


async def test_enqueues_when_no_live_transfer(monkeypatch):
    calls = _wire_transfers(monkeypatch, active_states=[None])
    await downloader._ensure_enqueued(CHOSEN)
    assert calls["enqueue"] == 1


async def test_refused_enqueue_attaches_when_row_appears(monkeypatch):
    # slskd's duplicate rejection races the first check: no row seen, enqueue
    # refused, second check finds the live transfer → attach, don't fail.
    calls = _wire_transfers(monkeypatch, active_states=[None, "Queued, Remotely"],
                            enqueue_ok=False)
    await downloader._ensure_enqueued(CHOSEN)
    assert calls["enqueue"] == 1
    assert calls["checks"] == 2


async def test_refused_enqueue_with_no_transfer_raises(monkeypatch):
    _wire_transfers(monkeypatch, active_states=[None, None], enqueue_ok=False)
    with pytest.raises(PeerTransferError, match="refused enqueue"):
        await downloader._ensure_enqueued(CHOSEN)


async def test_get_active_download_state_ignores_terminal_rows(monkeypatch):
    rows = [
        {"username": "peer", "directories": [{"files": [
            {"filename": "M\\A\\01 - Song.flac", "state": "Completed, Errored"},
            {"filename": "M\\A\\01 - Song.flac", "state": "InProgress"},
            {"filename": "M\\A\\02 - Other.flac", "state": "Queued, Remotely"},
        ]}]},
    ]
    fake = types.SimpleNamespace(transfers=types.SimpleNamespace(
        get_downloads=lambda username: rows[0],
        get_all_downloads=lambda: rows,
    ))
    monkeypatch.setattr(sc, "_client", fake)

    state = await sc.get_active_download_state("peer", "M\\A\\01 - Song.flac")
    assert state == "InProgress"  # terminal history row skipped

    done = [{"username": "peer", "directories": [{"files": [
        {"filename": "M\\A\\01 - Song.flac", "state": "Completed, Succeeded"},
    ]}]}]
    fake.transfers.get_downloads = lambda username: done[0]
    assert await sc.get_active_download_state("peer", "M\\A\\01 - Song.flac") is None
