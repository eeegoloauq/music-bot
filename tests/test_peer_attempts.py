"""How many peers a single track tries before giving up."""

import pytest

from soulseek import downloader
from soulseek.downloader import PeerTransferError

from conftest import make_result


def _peers(n):
    return [make_result(f"peer{i}", f"d\\{i:02d}.flac") for i in range(1, n + 1)]


def _fake_download(monkeypatch, good=None):
    attempts = []

    async def fake(chosen, *args, **kwargs):
        attempts.append(chosen.username)
        if chosen.username != good:
            raise PeerTransferError("transfer ended in state 'Completed, Errored'")
        return ("/x.flac", 1, "FLAC")

    monkeypatch.setattr(downloader, "_download_chosen", fake)
    return attempts


async def test_default_is_three_peers(monkeypatch, tmp_path):
    attempts = _fake_download(monkeypatch)
    with pytest.raises(PeerTransferError):
        await downloader._try_candidates(_peers(6), {"title": "t"}, {}, str(tmp_path), None, None)
    assert attempts == ["peer1", "peer2", "peer3"]


async def test_setting_raises_the_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader, "MAX_PEER_ATTEMPTS", 6)
    attempts = _fake_download(monkeypatch, good="peer5")
    res = await downloader._try_candidates(_peers(6), {"title": "t"}, {}, str(tmp_path), None, None)
    assert res[3].username == "peer5"  # the default cap would have stopped at peer3
    assert len(attempts) == 5


async def test_explicit_max_attempts_still_wins(monkeypatch, tmp_path):
    # Album tracks pass max_attempts=len(candidates); the setting must not cap them.
    monkeypatch.setattr(downloader, "MAX_PEER_ATTEMPTS", 1)
    attempts = _fake_download(monkeypatch, good="peer4")
    res = await downloader._try_candidates(
        _peers(4), {"title": "t"}, {}, str(tmp_path), None, None, max_attempts=4)
    assert res[3].username == "peer4"
    assert len(attempts) == 4
