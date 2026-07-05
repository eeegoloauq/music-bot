"""reporting.py: event folding, live render, honest failure text, caption caps.

Also covers the downloader's no-match reason selection (diag → user-facing
reason) and find_track's diagnostics for the "peers only had lossy rips" case
that used to be reported as a blanket "no Soulseek match".
"""

import reporting
from reporting import AlbumProgress, TrackProgress

from soulseek import matcher
from soulseek.downloader import _no_match_reason

from conftest import install_fake_client


# --- humanize_failure ---------------------------------------------------------

def test_humanize_maps_slskd_states():
    assert reporting.humanize_failure(
        "transfer ended in state 'Completed, TimedOut'"
    ) == "peer accepted but never sent the file"
    assert reporting.humanize_failure(
        "transfer ended in state 'Completed, Rejected'"
    ) == "peer rejected the transfer"
    assert reporting.humanize_failure(
        "slskd refused enqueue from someuser"
    ) == "peer refused the download request"


def test_humanize_passes_through_minted_reasons():
    assert reporting.humanize_failure("only lossy copies found (mp3 140 kbps)") == \
        "only lossy copies found (mp3 140 kbps)"
    assert reporting.humanize_failure("nothing found on Soulseek") == \
        "nothing found on Soulseek"


# --- no-match reason from diag --------------------------------------------------

def test_no_match_reason_prefers_lossy_over_nothing():
    assert _no_match_reason({"lossy_dropped": 3, "best_lossy_label": "mp3 140 kbps"}) \
        == "only lossy copies found (mp3 140 kbps)"
    assert _no_match_reason({}) == "nothing found on Soulseek"
    assert _no_match_reason({"files_seen": 4}) == \
        "files found but none matched this track"
    assert _no_match_reason({"usable_seen": 2, "files_seen": 2}) == \
        "files found but none matched this track"


async def test_find_track_diag_reports_lossy_only(monkeypatch):
    lossy_peer = [{
        "username": "mp3guy",
        "hasFreeUploadSlot": True,
        "uploadSpeed": 1_000_000,
        "queueLength": 0,
        "files": [{"filename": "Music\\Artist - Album\\01 - Song.mp3",
                   "size": 5_000_000, "length": 200, "bitRate": 140}],
    }]
    install_fake_client(monkeypatch, lambda i: lossy_peer)

    diag = {}
    auto, alts = await matcher.find_track(
        {"artist": "Artist", "title": "Song", "duration": 200}, diag=diag)

    assert auto is None and alts == []
    assert diag["usable_seen"] == 0
    assert diag["lossy_dropped"] > 0
    assert diag["best_lossy_label"] == "mp3 140 kbps"
    assert _no_match_reason(diag) == "only lossy copies found (mp3 140 kbps)"


# --- AlbumProgress --------------------------------------------------------------

def _plan(prog, titles, skipped=0):
    prog.handle({"t": "plan", "skipped": skipped, "total": len(titles) + skipped,
                 "tracks": [{"i": i, "title": t} for i, t in enumerate(titles, 1)]})


def test_album_progress_search_then_download_phases():
    prog = AlbumProgress("Artist", "Album", 3)
    _plan(prog, ["One", "Two", "Three"])

    assert prog.handle({"t": "search", "query": "artist album"}) is True
    text = prog.render()
    assert "Searching Soulseek" in text and "artist album" in text

    prog.handle({"t": "search_done", "query": "artist album", "files": 30, "peers": 4})
    assert "30 files / 4 peers" in prog.render()

    prog.handle({"t": "folder", "peer": "FatCJ", "covered": 3, "total": 3,
                 "quality": "16-bit/44kHz", "score": 87.2, "alts": 2})
    text = prog.render()
    assert "FatCJ" in text and "3/3" in text and "score 87" in text

    prog.handle({"t": "track", "i": 1, "state": "start", "title": "One"})
    prog.handle({"t": "track_progress", "i": 1, "pct": 45, "speed_bps": 2_097_152})
    text = prog.render()
    assert "⬇️ One" in text and "45%" in text and "2.0 MB/s" in text

    prog.handle({"t": "track", "i": 1, "state": "done", "title": "One"})
    prog.handle({"t": "track", "i": 2, "state": "fail", "title": "Two",
                 "reason": "transfer ended in state 'Completed, TimedOut'"})
    text = prog.render()
    assert "✅ One" in text
    assert "❌ Two — peer accepted but never sent the file" in text
    assert "▫️ Three" in text
    assert "1/3 saved" in text and "1 failed" in text


def test_album_progress_escapes_html():
    prog = AlbumProgress("<b>Artist</b>", "Al&bum", 1)
    _plan(prog, ["<script>"])
    prog.handle({"t": "track", "i": 1, "state": "start", "title": "<script>"})
    text = prog.render()
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert "<b>Artist</b>" not in text.replace("<b>", "", 1)  # only our own bold tag


def test_album_progress_compact_checklist_for_huge_albums():
    titles = [f"Track {n}" for n in range(1, 61)]
    prog = AlbumProgress("A", "B", 60)
    _plan(prog, titles)
    for i in range(1, 31):
        prog.handle({"t": "track", "i": i, "state": "done", "title": titles[i - 1]})
    prog.handle({"t": "track", "i": 31, "state": "start", "title": titles[30]})
    text = prog.render()
    assert len(text) < reporting.MESSAGE_LIMIT
    assert "30 done" in text and "⬇️ Track 31" in text


# --- TrackProgress ---------------------------------------------------------------

def test_track_progress_match_and_speed():
    prog = TrackProgress("Artist", "Song")
    prog.handle({"t": "search", "query": "artist song"})
    assert "Searching Soulseek" in prog.render()
    prog.handle({"t": "search_wait", "secs": 30})
    assert "pacing" in prog.render()

    prog.handle({"t": "match", "peer": "gooduser", "quality": "FLAC 16-bit 44kHz",
                 "score": 51.0, "copies": 4})
    prog.handle({"t": "track", "i": 1, "state": "start", "title": "Song"})
    prog.handle({"t": "track_progress", "i": 1, "pct": 78, "speed_bps": 512_000})
    text = prog.render()
    assert "gooduser" in text and "FLAC" in text and "match 51" in text
    assert "78%" in text and "500 KB/s" in text

    prog.handle({"t": "track", "i": 1, "state": "done", "fmt": "FLAC", "peer": "gooduser"})
    assert prog.peer == "gooduser"


# --- final summaries --------------------------------------------------------------

def _result(**kw):
    base = dict(album_dir="/x", downloaded=0, skipped=0, failed=[], total=0,
                format="", with_lyrics=0, elapsed_secs=0, source_counts={})
    base.update(kw)
    return base


def test_album_final_lists_failures_with_reasons():
    res = _result(
        downloaded=10, total=12, format="FLAC 16-bit 44kHz", with_lyrics=8,
        elapsed_secs=272, source_counts={"FatCJ": 9, "xtdeck": 1},
        failed=[("Oh So Very Based", "only lossy copies found (mp3 140 kbps)"),
                ("Ankles Cuffed", "transfer ended in state 'Completed, TimedOut'")],
    )
    text = reporting.render_album_final("Acid Souljah", "$wagSouljah", res,
                                        scan_note="Library scan triggered.",
                                        share_url="https://navi/share/x")
    assert text.startswith("⚠️ <b>Acid Souljah — $wagSouljah</b>")
    assert "10/12 saved" in text
    assert "from FatCJ ×9, xtdeck" in text
    assert "Oh So Very Based — only lossy copies found (mp3 140 kbps)" in text
    assert "Ankles Cuffed — peer accepted but never sent the file" in text
    assert "Library scan triggered." in text and "https://navi/share/x" in text
    assert len(text) <= reporting.CAPTION_LIMIT


def test_album_final_groups_many_failures_and_fits_caption():
    failed = [(f"Some Pretty Long Track Title {n}", "nothing found on Soulseek")
              for n in range(9)]
    failed += [(f"Other Track {n}", "only lossy copies found (mp3 128 kbps)")
               for n in range(4)]
    res = _result(downloaded=1, total=14, failed=failed, format="FLAC")
    text = reporting.render_album_final("A", "B", res, scan_note="scan.")
    assert len(text) <= reporting.CAPTION_LIMIT
    assert "13 not downloaded" in text
    assert "nothing found on Soulseek" in text
    assert "+6 more" in text  # 9 shown as 3 + "+6 more"


def test_album_final_all_saved_is_a_clean_success():
    res = _result(downloaded=12, total=12, format="FLAC 16-bit 44kHz",
                  elapsed_secs=120, source_counts={"peer": 12})
    text = reporting.render_album_final("A", "B", res)
    assert text.startswith("✅")
    assert "12/12 saved" in text and "not downloaded" not in text


def test_track_final():
    text = reporting.render_track_final(
        "Artist", "Song", "FLAC 16-bit 44.1kHz", "gooduser", 42,
        scan_note="Library scan triggered.", share_url="https://navi/share/y")
    assert text.startswith("✅ <b>Artist — Song</b>")
    assert "FLAC" in text and "from gooduser" in text and "42s" in text
