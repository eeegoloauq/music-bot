"""Local-upload intake: unpack safety, caps, filtering, folder passthrough.

Everything runs on tmp_path — no bot, no network. The async watch loop is a
thin shell around ``_scan_stable`` + ``_process_entry``, so those are what
get exercised.
"""

import os
import zipfile

import pytest

import uploads
from uploads import IntakeReport, _process_entry, _scan_stable, format_rejection


def make_zip(path, members):
    """members: {arcname: bytes}"""
    with zipfile.ZipFile(path, "w") as zf:
        for arcname, data in members.items():
            zf.writestr(arcname, data)


def staged_files(report):
    out = []
    for root, _dirs, files in os.walk(report.staging_dir):
        out += [os.path.relpath(os.path.join(root, f), report.staging_dir) for f in files]
    return sorted(out)


def test_happy_zip_unpack(tmp_path):
    zip_path = tmp_path / "Album - Artist.zip"
    make_zip(zip_path, {
        "Album - Artist/01 - Song.flac": b"F" * 100,
        "Album - Artist/cover.jpg": b"J" * 10,
        "Album - Artist/playlist.m3u": b"m3u",
    })
    r = _process_entry(str(zip_path), str(tmp_path))

    assert r.error is None
    assert r.audio == ["Album - Artist/01 - Song.flac"]
    assert r.art == ["Album - Artist/cover.jpg"]
    assert r.skipped == ["Album - Artist/playlist.m3u"]
    assert not zip_path.exists()  # consumed
    assert staged_files(r) == [os.path.join("Album - Artist", "01 - Song.flac"),
                               os.path.join("Album - Artist", "cover.jpg")]


def test_zip_slip_rejected(tmp_path):
    upload_dir = tmp_path / "up"
    upload_dir.mkdir()
    zip_path = upload_dir / "evil.zip"
    make_zip(zip_path, {"../escaped.flac": b"x", "ok.flac": b"x"})

    r = _process_entry(str(zip_path), str(upload_dir))

    assert r.error and "unsafe path" in r.error
    assert not (upload_dir / ".extracted").exists() or not os.listdir(upload_dir / ".extracted")
    assert not (tmp_path / "escaped.flac").exists()
    assert (upload_dir / ".rejected" / "evil.zip").exists()  # parked, not re-chewed


def test_total_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_MAX_TOTAL_BYTES", 150)
    zip_path = tmp_path / "big.zip"
    make_zip(zip_path, {"a.flac": b"x" * 100, "b.flac": b"x" * 100})

    r = _process_entry(str(zip_path), str(tmp_path))

    assert r.error and "size cap" in r.error
    assert (tmp_path / ".rejected" / "big.zip").exists()


def test_per_file_cap_beats_lying_header(tmp_path, monkeypatch):
    # cap enforced on streamed bytes; a single oversized member kills it
    monkeypatch.setattr(uploads, "MAX_FILE_BYTES", 50)
    zip_path = tmp_path / "fat.zip"
    make_zip(zip_path, {"a.flac": b"x" * 100})

    r = _process_entry(str(zip_path), str(tmp_path))
    assert r.error and "size cap" in r.error


def test_zip_without_audio_rejected(tmp_path):
    zip_path = tmp_path / "junk.zip"
    make_zip(zip_path, {"readme.txt": b"hi", "cover.jpg": b"J"})

    r = _process_entry(str(zip_path), str(tmp_path))
    assert r.error == "no audio files inside"
    assert (tmp_path / ".rejected" / "junk.zip").exists()


def test_dotfile_members_skipped(tmp_path):
    zip_path = tmp_path / "mac.zip"
    make_zip(zip_path, {"__MACOSX/._01 - Song.flac": b"apple",
                        "01 - Song.flac": b"F" * 10})

    r = _process_entry(str(zip_path), str(tmp_path))
    assert r.audio == ["01 - Song.flac"]
    assert r.skipped == ["__MACOSX/._01 - Song.flac"]


def test_plain_folder_passthrough(tmp_path):
    src = tmp_path / "My Album"
    (src / "sub").mkdir(parents=True)
    (src / "01 - a.mp3").write_bytes(b"m" * 20)
    (src / "sub" / "cover.png").write_bytes(b"p")
    (src / "notes.txt").write_bytes(b"n")

    r = _process_entry(str(src), str(tmp_path))

    assert r.error is None
    assert r.audio == ["01 - a.mp3"]
    assert r.art == [os.path.join("sub", "cover.png")]
    assert not src.exists()  # moved wholesale into staging
    assert "notes.txt" in staged_files(r)  # non-audio travels with the folder


def test_folder_without_audio_rejected(tmp_path):
    src = tmp_path / "pics"
    src.mkdir()
    (src / "cover.jpg").write_bytes(b"j")

    r = _process_entry(str(src), str(tmp_path))
    assert r.error == "no audio files inside"
    assert (tmp_path / ".rejected" / "pics").exists()


def test_loose_audio_file_staged(tmp_path):
    f = tmp_path / "song.flac"
    f.write_bytes(b"F" * 10)

    r = _process_entry(str(f), str(tmp_path))
    assert r.error is None and r.audio == ["song.flac"]
    assert not f.exists()
    assert staged_files(r) == ["song.flac"]


def test_loose_junk_rejected(tmp_path):
    f = tmp_path / "virus.exe"
    f.write_bytes(b"MZ")

    r = _process_entry(str(f), str(tmp_path))
    assert r.error == "not a zip, folder, or audio file"
    assert (tmp_path / ".rejected" / "virus.exe").exists()


def test_scan_stability_two_ticks(tmp_path):
    (tmp_path / ".extracted").mkdir()  # dot-dirs are invisible to the scan
    f = tmp_path / "drop.zip"
    f.write_bytes(b"x" * 10)

    ready, sizes = _scan_stable(str(tmp_path), {})
    assert ready == []  # first sighting: never ready

    f.write_bytes(b"x" * 20)  # still being copied
    ready, sizes = _scan_stable(str(tmp_path), sizes)
    assert ready == []

    ready, _ = _scan_stable(str(tmp_path), sizes)  # size held still
    assert ready == [str(f)]


def test_scan_folder_size_recursive(tmp_path):
    d = tmp_path / "album"
    d.mkdir()
    (d / "a.flac").write_bytes(b"x" * 10)

    _, sizes = _scan_stable(str(tmp_path), {})
    (d / "b.flac").write_bytes(b"x" * 10)  # copy still in progress inside dir
    ready, sizes2 = _scan_stable(str(tmp_path), sizes)
    assert ready == [] and sizes2["album"] == 20


def test_format_rejection():
    bad = IntakeReport(name="y.zip", error="size cap exceeded at a.flac")
    assert "rejected" in format_rejection(bad) and "size cap" in format_rejection(bad)
