"""Characterization tests for the slow-source-recovery series
(docs/slow-source-recovery.md). Each pins *today's* behavior so the series'
commits flip exactly one expectation each: the speed-blind folder walk flips
with the downloader-wiring commit, the upload abort-on-edit with the
safe-edit commit; the rest must keep passing throughout.

The album driver runs ``download_album`` against a scripted per-(peer, track)
outcome table and a fake clock, so per-track transfer speed is fully
controlled without sleeping.
"""

import types

import pytest
from telegram.error import NetworkError

import bot
import uploads
from soulseek import downloader
from soulseek.downloader import PeerTransferError
from soulseek.selection import order_for_download, pick_quality_locked

from conftest import make_result


# --- album-walk driver ------------------------------------------------------

def make_album(n_tracks: int) -> dict:
    return {
        "artist": "Artist", "title": "Album",
        "tracks": [{"id": str(i), "title": f"Track {i}", "artist": "Artist",
                    "duration": 200, "discNumber": 1, "trackNumber": i}
                   for i in range(1, n_tracks + 1)],
    }


def make_folder(username: str, album: dict, *, score: float = 80.0,
                bit_depth: int = 16, sample_rate: int = 44100,
                missing: set[str] = frozenset()):
    """Duck-typed ScoredFolder covering every track of ``album`` except the
    titles in ``missing``."""
    files = [make_result(username,
                         f"M\\Artist - Album\\{t['trackNumber']:02d} - {t['title']}.flac",
                         bit_depth=bit_depth, sample_rate=sample_rate)
             if t["title"] not in missing else None
             for t in album["tracks"]]
    return types.SimpleNamespace(
        folder=types.SimpleNamespace(username=username),
        score=score, missing_count=len(missing), matched_files=files)


class Harness:
    """Wires download_album's collaborators to a scripted outcome table.

    ``script[(username, track_title)]`` is ``(size_bytes, elapsed_secs)`` for
    a success or the string ``"fail"``; every actual transfer attempt lands
    in ``attempts`` as ``(username, track_title)``.
    """

    def __init__(self, monkeypatch, script):
        self.script = script
        self.attempts: list[tuple[str, str]] = []
        # Pin the slow-source floor to its documented default regardless of
        # the local env (docs/slow-source-recovery.md §A).
        monkeypatch.setattr(downloader, "SLOW_SOURCE_MIN_MBPS", 0.5)
        clock = types.SimpleNamespace(_t=0.0)
        clock.monotonic = lambda: clock._t
        self.clock = clock
        monkeypatch.setattr(downloader, "time", clock)

        async def fake_download_chosen(chosen, track, album, album_dir,
                                       cover_data, lyrics_task,
                                       on_progress=None):
            self.attempts.append((chosen.username, track["title"]))
            outcome = self.script[(chosen.username, track["title"])]
            if outcome == "fail":
                raise PeerTransferError("transfer ended in state 'Completed, TimedOut'")
            size, secs = outcome
            clock._t += secs
            return f"{album_dir}/{track['title']}.flac", size, "FLAC 16-bit 44kHz"

        async def fake_lyrics(*a, **k):
            return None

        async def fake_remove_completed():
            return None

        monkeypatch.setattr(downloader, "_download_chosen", fake_download_chosen)
        monkeypatch.setattr(downloader, "fetch_lyrics", fake_lyrics)
        monkeypatch.setattr(downloader.slskd, "remove_completed_downloads",
                            fake_remove_completed)

    def set_folders(self, monkeypatch, best, alternatives, pool=()):
        async def fake_find_album(album, on_event=None):
            return best, list(alternatives), list(pool)
        monkeypatch.setattr(downloader, "find_album", fake_find_album)

    def set_track_search(self, monkeypatch, results_by_title):
        """Per-track fallback search: title -> list of SearchResults (first
        one is the ranked pick, the rest alternatives)."""
        async def fake_find_track(track, **kwargs):
            picks = results_by_title.get(track["title"], [])
            entries = [types.SimpleNamespace(result=r, sources=[r]) for r in picks]
            return (entries[0] if entries else None), entries[1:]
        monkeypatch.setattr(downloader, "find_track", fake_find_track)


MB = 1024 * 1024


async def test_slow_walk_switches_to_earned_fallback(monkeypatch, tmp_path, caplog):
    """Flipped by the slow-source wiring commit (docs/slow-source-recovery.md
    §A): this used to pin the 2026-07-20 incident, where the walk was
    speed-blind and a ~0.1 MB/s folder peer kept the whole album while a
    same-quality fallback sat unused in the chain. Now two qualifying tracks
    below the floor earn a switch to the fallback covering all remaining."""
    album = make_album(4)
    script = {("slowfolk", t["title"]): (20 * MB, 200.0) for t in album["tracks"]}
    script.update({("fastalt", t["title"]): (20 * MB, 10.0) for t in album["tracks"]})
    h = Harness(monkeypatch, script)
    h.set_folders(monkeypatch, make_folder("slowfolk", album),
                  [make_folder("fastalt", album)])

    with caplog.at_level("INFO", logger="soulseek.downloader"):
        result = await downloader.download_album("1", str(tmp_path), album=album)

    assert result["downloaded"] == 4 and not result["failed"]
    assert result["source_counts"] == {"slowfolk": 2, "fastalt": 2}
    decisions = [r.message for r in caplog.records if "Slow source" in r.message]
    assert len(decisions) == 1 and "switching to peer fastalt" in decisions[0]
    assert "covers 2/2 remaining @ 16-bit/44kHz" in decisions[0]


async def test_slow_walk_stays_without_eligible_fallback(monkeypatch, tmp_path, caplog):
    """No same-quality full-coverage fallback → staying is the correct
    outcome, logged exactly once (the walk settles; no per-track re-log)."""
    album = make_album(4)
    script = {("slowfolk", t["title"]): (20 * MB, 200.0) for t in album["tracks"]}
    h = Harness(monkeypatch, script)
    h.set_folders(monkeypatch, make_folder("slowfolk", album),
                  [make_folder("hiresalt", album, bit_depth=24,
                               sample_rate=96_000)])  # breaks the lock

    with caplog.at_level("INFO", logger="soulseek.downloader"):
        result = await downloader.download_album("1", str(tmp_path), album=album)

    assert result["downloaded"] == 4 and not result["failed"]
    assert result["source_counts"] == {"slowfolk": 4}
    decisions = [r.message for r in caplog.records if "Slow source" in r.message]
    assert len(decisions) == 1 and "staying" in decisions[0]


async def test_switch_cap_and_measured_back_switch(monkeypatch, tmp_path):
    """Two slow folders: the album settles on the better of them. The
    back-switch is allowed only because the first peer measured faster, and
    the switch cap (2/album) ends the shuffling there."""
    album = make_album(6)
    script = {("slowfolk", t["title"]): (20 * MB, 200.0) for t in album["tracks"]}
    script.update({("slower", t["title"]): (20 * MB, 400.0) for t in album["tracks"]})
    h = Harness(monkeypatch, script)
    h.set_folders(monkeypatch, make_folder("slowfolk", album),
                  [make_folder("slower", album)])

    result = await downloader.download_album("1", str(tmp_path), album=album)

    assert result["downloaded"] == 6 and not result["failed"]
    assert h.attempts == [
        ("slowfolk", "Track 1"), ("slowfolk", "Track 2"),  # 0.1 → verdict
        ("slower", "Track 3"), ("slower", "Track 4"),      # 0.05 → worse
        ("slowfolk", "Track 5"), ("slowfolk", "Track 6"),  # back; cap ends it
    ]


async def test_floor_zero_disables_switching(monkeypatch, tmp_path):
    """SLOW_SOURCE_MIN_MBPS=0 restores the pre-series speed-blind walk."""
    album = make_album(4)
    script = {("slowfolk", t["title"]): (20 * MB, 200.0) for t in album["tracks"]}
    script.update({("fastalt", t["title"]): (20 * MB, 10.0) for t in album["tracks"]})
    h = Harness(monkeypatch, script)
    monkeypatch.setattr(downloader, "SLOW_SOURCE_MIN_MBPS", 0.0)
    h.set_folders(monkeypatch, make_folder("slowfolk", album),
                  [make_folder("fastalt", album)])

    result = await downloader.download_album("1", str(tmp_path), album=album)

    assert result["source_counts"] == {"slowfolk": 4}


async def test_gap_fill_demotes_measured_slow_peer(monkeypatch, tmp_path):
    """A measured-slow peer's files stay eligible in per-track gap-fill but
    move behind other peers' copies — last resort, not first pick."""
    album = make_album(4)
    folder = make_folder("slowfolk", album, missing={"Track 4"})  # 3/4 ≥ 0.75
    script = {("slowfolk", t["title"]): (20 * MB, 200.0) for t in album["tracks"]}
    script[("other", "Track 4")] = (20 * MB, 10.0)
    h = Harness(monkeypatch, script)
    h.set_folders(monkeypatch, folder, [])
    # The gap-fill search ranks the slow peer's copy first; demotion must
    # put the fresh peer ahead of it.
    h.set_track_search(monkeypatch, {
        "Track 4": [make_result("slowfolk", "M\\Artist - Album\\04 - Track 4.flac"),
                    make_result("other", "S\\Artist - Album\\04 - Track 4.flac")],
    })

    result = await downloader.download_album("1", str(tmp_path), album=album)

    assert result["downloaded"] == 4 and not result["failed"]
    assert h.attempts == [
        ("slowfolk", "Track 1"), ("slowfolk", "Track 2"), ("slowfolk", "Track 3"),
        ("other", "Track 4"),
    ]


async def test_failed_track_retried_after_primary_walk_completes(monkeypatch, tmp_path):
    """A track that fails on the primary folder peer is picked up from the
    next chain folder only after the primary's walk finishes — the incident's
    track 1 finishing last."""
    album = make_album(3)
    script = {
        ("primary", "Track 1"): "fail",
        ("primary", "Track 2"): (20 * MB, 10.0),
        ("primary", "Track 3"): (20 * MB, 10.0),
        ("backup", "Track 1"): (20 * MB, 10.0),
    }
    h = Harness(monkeypatch, script)
    h.set_folders(monkeypatch, make_folder("primary", album),
                  [make_folder("backup", album)])

    result = await downloader.download_album("1", str(tmp_path), album=album)

    assert result["downloaded"] == 3 and not result["failed"]
    assert h.attempts == [
        ("primary", "Track 1"), ("primary", "Track 2"), ("primary", "Track 3"),
        ("backup", "Track 1"),
    ]


async def test_peer_abandon_k_counts_consecutive_failures(monkeypatch, tmp_path):
    """Two consecutive failures abandon the folder peer; a success in between
    resets the counter — so the abandonment fires on tracks 3+4, not 1+3."""
    album = make_album(4)
    script = {
        ("flaky", "Track 1"): "fail",
        ("flaky", "Track 2"): (20 * MB, 10.0),
        ("flaky", "Track 3"): "fail",
        ("flaky", "Track 4"): "fail",
        ("backup", "Track 1"): (20 * MB, 10.0),
        ("backup", "Track 3"): (20 * MB, 10.0),
        ("backup", "Track 4"): (20 * MB, 10.0),
    }
    h = Harness(monkeypatch, script)
    h.set_folders(monkeypatch, make_folder("flaky", album),
                  [make_folder("backup", album)])

    result = await downloader.download_album("1", str(tmp_path), album=album)

    assert result["downloaded"] == 4 and not result["failed"]
    assert h.attempts == [
        ("flaky", "Track 1"), ("flaky", "Track 2"), ("flaky", "Track 3"),
        ("flaky", "Track 4"),  # 2nd consecutive failure → abandoned here
        ("backup", "Track 1"), ("backup", "Track 3"), ("backup", "Track 4"),
    ]


def test_quality_lock_ordering():
    """order_for_download promotes the lock-matching candidate to the front;
    pick_quality_locked falls back to picks[0] when nothing matches."""
    hires = make_result("p", "M\\A\\01.flac", bit_depth=24, sample_rate=96_000)
    cd = make_result("q", "M\\A\\01.flac")
    auto = types.SimpleNamespace(result=hires, sources=[hires])
    alt = types.SimpleNamespace(result=cd, sources=[cd])

    assert order_for_download(auto, [alt], (16, 44100)) == [cd, hires]
    assert order_for_download(auto, [alt]) == [hires, cd]  # no lock: rank order
    assert pick_quality_locked([hires], (16, 44100)) is hires  # fallback: picks[0]


async def test_failed_candidate_keys_cross_phases(monkeypatch, tmp_path):
    """A (peer, file) that failed in the folder phase is not re-attempted in
    the per-track fallback, even when the search surfaces it again."""
    album = make_album(2)
    folder = make_folder("primary", album)
    script = {
        ("primary", "Track 1"): (20 * MB, 10.0),
        ("primary", "Track 2"): "fail",
        ("other", "Track 2"): (20 * MB, 10.0),
    }
    h = Harness(monkeypatch, script)
    h.set_folders(monkeypatch, folder, [])
    # The per-track search re-surfaces the exact file that just failed, ahead
    # of a fresh peer's copy.
    h.set_track_search(monkeypatch, {
        "Track 2": [folder.matched_files[1],
                    make_result("other", "S\\Artist - Album\\02 - Track 2.flac")],
    })

    result = await downloader.download_album("1", str(tmp_path), album=album)

    assert result["downloaded"] == 2 and not result["failed"]
    assert h.attempts == [
        ("primary", "Track 1"), ("primary", "Track 2"),  # folder phase
        ("other", "Track 2"),                            # known-bad key skipped
    ]


# --- upload abort-on-edit (incident B) --------------------------------------

class RaisingMessage:
    """Status message whose every edit fails like a ~30s network blip."""
    message_id = 7

    def __init__(self):
        self.edit_calls = 0

    async def edit_text(self, *args, **kwargs):
        self.edit_calls += 1
        raise NetworkError("httpx.ReadError")


class FakeUploadIO:
    def __init__(self, msg):
        self._msg = msg
        self.sent: list[str] = []

    async def reply_text(self, text):
        self.sent.append(text)
        return self._msg

    async def reply_photo(self, photo, caption):
        raise AssertionError("not used in these tests")


async def test_network_error_on_cosmetic_edit_no_longer_aborts_upload(
        monkeypatch, tmp_path):
    """Flipped by the safe-edit commit (docs/slow-source-recovery.md §B):
    this used to pin the incident bug, where a transient NetworkError from
    the cosmetic "Importing upload…" edit aborted _handle_upload before
    import_staged_album ever ran. Now every status edit is best-effort — the
    import files the album even when the chat is unreachable throughout.
    """
    imported = []

    async def fake_identify(staging_dir, name):
        return "42"

    async def fake_fetch(album_id):
        return {"artist": "Artist", "title": "Album", "tracks": []}

    async def fake_enrich(album):
        return None

    async def fake_import(album, staging_dir, music_dir):
        imported.append(album)
        return {"album_dir": str(tmp_path), "downloaded": 1, "skipped": 0,
                "failed": [], "total": 1, "format": "FLAC", "with_lyrics": 0}

    async def fake_scan():
        return "scanned"

    async def fake_share(artist, title, skip_delay=False):
        return None

    monkeypatch.setattr(bot.upload_import, "identify_album", fake_identify)
    monkeypatch.setattr(bot.upload_import, "import_staged_album", fake_import)
    monkeypatch.setattr(bot.metadata, "fetch_album", fake_fetch)
    monkeypatch.setattr(bot.metadata, "enrich_genres", fake_enrich)
    monkeypatch.setattr(bot, "_trigger_scan", fake_scan)
    monkeypatch.setattr(bot, "_try_share_album", fake_share)
    monkeypatch.setattr(bot, "_EDIT_BACKOFF", (0,))
    monkeypatch.setattr(bot, "_FINAL_BACKOFF", (0,))

    report = uploads.IntakeReport(name="drop.zip", staging_dir=str(tmp_path),
                                  audio=["01 - Track 1.flac"])
    msg = RaisingMessage()

    await bot._handle_upload(FakeUploadIO(msg), report)  # must not raise

    assert len(imported) == 1        # filing work ran despite the dead chat
    assert msg.edit_calls >= 2       # the edits were attempted (and retried)
