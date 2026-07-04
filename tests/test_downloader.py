"""Peer-retry bookkeeping in ``soulseek.downloader``: failed peers are
remembered across an album's phases and never re-attempted."""

import pytest

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
