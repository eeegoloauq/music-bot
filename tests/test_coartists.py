import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

import retagger
from library import tagger
from metadata import api, deezer


@pytest.mark.parametrize(
    "contributors, expected_artists, expected_co, expected_featured",
    [
        (
            [
                {"name": "Artist A", "role": "Main"},
                {"name": "Artist B", "role": "Main"},
            ],
            [
                {"name": "Artist A", "role": "Main"},
                {"name": "Artist B", "role": "Main"},
            ],
            ["Artist B"],
            [],
        ),
        (
            [
                {"name": "Artist A", "role": "Main"},
                {"name": "Guest", "role": "Featured"},
            ],
            [
                {"name": "Artist A", "role": "Main"},
                {"name": "Guest", "role": "Featured"},
            ],
            [],
            ["Guest"],
        ),
        ([], [], [], []),
        (
            [
                {"name": "artist a", "role": "Main"},
                {"name": "ARTIST A", "role": "Featured"},
            ],
            [{"name": "artist a", "role": "Main"}],
            [],
            [],
        ),
    ],
)
def test_album_contributors_are_adapted(
    contributors, expected_artists, expected_co, expected_featured,
):
    summary = {"id": 1, "title": "Song", "artist": {"name": "Artist A"}}
    album = api._adapt_album(
        {"id": 2, "artist": {"name": "Album Artist"}, "tracks": {"data": [summary]}},
        [{"id": 1, "contributors": contributors}],
    )

    adapted = album["tracks"][0]
    assert adapted["artists"] == expected_artists
    assert adapted["coArtists"] == expected_co
    assert adapted["featuredArtists"] == expected_featured


async def test_single_track_contributors_are_adapted(monkeypatch):
    monkeypatch.setattr(
        deezer,
        "get_track",
        AsyncMock(return_value={
            "id": 1,
            "title": "Song",
            "artist": {"name": "Artist A"},
            "album": {},
            "contributors": [
                {"name": "Artist A", "role": "Main"},
                {"name": "Artist B", "role": "Main"},
                {"name": "Guest", "role": "Featured"},
            ],
        }),
    )

    track, _ = await api.fetch_single_track("1")

    assert track["coArtists"] == ["Artist B"]
    assert track["featuredArtists"] == ["Guest"]


@pytest.mark.parametrize(
    "track, expected",
    [
        ({"artist": "A"}, "A"),
        ({"artist": "A", "coArtists": ["B"]}, "A & B"),
        ({"artist": "A", "coArtists": ["B", "C"]}, "A, B & C"),
        ({"artist": "A", "featuredArtists": ["X", "Y"]}, "A feat. X, Y"),
        (
            {"artist": "A", "coArtists": ["B", "a"],
             "featuredArtists": ["b", "X", "x"]},
            "A & B feat. X",
        ),
    ],
)
def test_format_artist(track, expected):
    assert tagger._format_artist(track, {}) == expected


def test_retag_plan_detects_missing_artists_and_changed_display_artist(monkeypatch):
    summary = {
        "path": "/music/A/Album/01.flac",
        "ext": ".flac",
        "comment": "music-bot · https://www.deezer.com/album/2",
        "genres": [],
        "artist": "A",
        "album": "Album",
        "albumartist": "A",
        "releasedate": "2026-01-01",
        "has_rg": False,
        "isrc": "ISRC1",
        "tracknumber": 1,
        "title": "Song",
        "has_artists": False,
    }
    monkeypatch.setattr(retagger, "_read_file_summary", lambda _path: summary)
    track, album = _metadata()
    track["isrc"] = "ISRC1"
    album.update({"releaseDate": "2026-01-01", "tracks": [track]})

    changes, _, _ = retagger._compute_changes(
        "/music/A/Album", "A", "Album", album, [summary["path"]],
    )

    assert "artists: 1/1 missing" in changes
    assert "track artist: 1/1 differs" in changes


def _make_audio(path):
    command = [
        "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
        "anullsrc=r=44100:cl=mono", "-t", "0.2", "-map_metadata", "-1",
    ]
    if path.suffix == ".m4a":
        command.extend(["-c:a", "aac"])
    command.append(str(path))
    subprocess.run(command, check=True)


def _metadata():
    return (
        {
            "id": "1", "title": "Song", "artist": "A", "coArtists": ["B"],
            "featuredArtists": ["X"], "trackNumber": 1, "discNumber": 1,
        },
        {"id": "2", "title": "Album", "artist": "A", "numberOfTracks": 1},
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
async def test_flac_artists_round_trip_and_patch(tmp_path):
    path = tmp_path / "song.flac"
    _make_audio(path)
    track, album = _metadata()
    tagger._write_tags(str(path), track, album, None, None, force=True)

    audio = FLAC(path)
    assert audio["artist"] == ["A & B feat. X"]
    assert audio["artists"] == ["A", "B", "X"]
    assert audio["albumartist"] == ["A"]
    assert audio["title"] == ["Song"]

    del audio["artists"]
    audio["artist"] = "wrong"
    audio.save()
    added = await tagger._patch_missing_tags(str(path), track, album)

    patched = FLAC(path)
    assert patched["artists"] == ["A", "B", "X"]
    assert patched["artist"] == ["A & B feat. X"]
    assert patched["title"] == ["Song"]
    assert "artists" in added
    assert "artist" in added

    del patched["artists"]
    patched.save()
    assert retagger._retag_flac_surgical(str(path), track, album)
    assert FLAC(path)["artists"] == ["A", "B", "X"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
@pytest.mark.parametrize("suffix", [".mp3", ".m4a"])
def test_lossy_artists_tag_structure(tmp_path, suffix):
    path = tmp_path / f"song{suffix}"
    _make_audio(path)
    track, album = _metadata()

    if suffix == ".mp3":
        tagger._write_mp3_tags(str(path), track, album, None, None, force=True)
        frames = [f for f in ID3(path).getall("TXXX") if f.desc == "ARTISTS"]
        assert len(frames) == 1
        assert frames[0].text == ["A", "B", "X"]
    else:
        tagger._write_m4a_tags(str(path), track, album, None, None, force=True)
        values = MP4(path)["----:com.apple.iTunes:ARTISTS"]
        assert [bytes(value).decode() for value in values] == ["A", "B", "X"]


def test_album_level_contributors_are_not_track_guests():
    """Deezer's "I Hate Rap": a producer credited Main on the album and on
    every track is not a per-track co-artist — the site's own listing shows
    only the extra names (RXKNephew, Miss Bashful), and so do we."""
    tracks = [
        {"id": 1, "contributors": [
            {"name": "CHRIST DILLINGER", "role": "Main"},
            {"name": "Rosaliedu38", "role": "Main"},
            {"name": "RxkNephew", "role": "Featured"}]},
        {"id": 2, "contributors": [
            {"name": "CHRIST DILLINGER", "role": "Main"},
            {"name": "Rosaliedu38", "role": "Main"},
            {"name": "Miss Bashful", "role": "Main"}]},
    ]
    album = api._adapt_album(
        {"id": 9, "artist": {"name": "CHRIST DILLINGER"},
         "contributors": [{"name": "CHRIST DILLINGER", "role": "Main"},
                          {"name": "Rosaliedu38", "role": "Main"}],
         "tracks": {"data": [{"id": 1, "title": "Never Trust You Again",
                              "artist": {"name": "CHRIST DILLINGER"}},
                             {"id": 2, "title": "I Do What I Want",
                              "artist": {"name": "CHRIST DILLINGER"}}]}},
        tracks,
    )
    assert album["contributors"] == ["CHRIST DILLINGER", "Rosaliedu38"]
    t1, t2 = album["tracks"]
    assert (t1["coArtists"], t1["featuredArtists"]) == ([], ["RxkNephew"])
    assert (t2["coArtists"], t2["featuredArtists"]) == (["Miss Bashful"], [])
