"""Untagged files (zips from the wild) match Deezer tracks by filename:
"NN. Artist - Title.flac" carries the number and the title even when the
tags are empty — the 21.09 case, 11 FLACs with no tags at all."""

import shutil
import subprocess

import pytest

from library.files import _find_existing_track, _guess_from_filename


@pytest.mark.parametrize("fname, num, titles", [
    ("01. Christ Dillinger - Drunk af.flac", "1", {"christ dillinger - drunk af", "drunk af"}),
    ("03. Christ Dillinger - Who Went To The Island?.flac", "3",
     {"christ dillinger - who went to the island?", "who went to the island?"}),
    ("1-02 Title.flac", "2", {"title"}),
    ("07 - Title.mp3", "7", {"title"}),
    ("Artist - Title.flac", "0", {"artist - title", "title"}),
    ("01. Artist - Song - Live.flac", "1", {"artist - song - live", "song - live", "live"}),
    ("1985.flac", "0", {"1985"}),
    ("07-Title.flac", "7", {"title"}),
])
def test_guess_from_filename(fname, num, titles):
    assert _guess_from_filename(fname) == (num, titles)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_untagged_flac_matches_by_filename(tmp_path):
    for name in ("03. Christ Dillinger - Who Went To The Island_.flac",
                 "04. Christ Dillinger - Depression Diss Track (depressionK).flac"):
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
             "-t", "0.2", "-map_metadata", "-1", "-fflags", "+bitexact", str(tmp_path / name)],
            check=True)

    hit = _find_existing_track(str(tmp_path), {
        "title": "Who Went To The Island?", "trackNumber": 3, "discNumber": 1})
    assert hit and hit.endswith("Island_.flac")   # "?" can't be in a filename

    hit = _find_existing_track(str(tmp_path), {
        "title": "Depression Diss Track (depressionK)", "trackNumber": 4})
    assert hit and hit.endswith("(depressionK).flac")

    assert _find_existing_track(str(tmp_path), {"title": "Love Yourself", "trackNumber": 5}) is None
