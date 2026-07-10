"""Upload identify ladder + staged-album import. Offline: Deezer/resolver/
tagger calls are monkeypatched, the filesystem is real (tmp_path)."""

import os

import pytest

import upload_import
from upload_import import (
    _read_signals, identify_album, import_staged_album, _stringify,
)


SIGNALS_EMPTY = {"urls": [], "isrcs": [], "upcs": [], "artist": "", "album": ""}


def set_signals(monkeypatch, **overrides):
    sig = {**SIGNALS_EMPTY, **overrides}
    monkeypatch.setattr(upload_import, "_read_signals", lambda _dir: sig)
    return sig


async def _fail(*a, **k):
    raise AssertionError("must not be called")


# --- identify ladder --------------------------------------------------------

async def test_url_rung_wins(monkeypatch):
    set_signals(monkeypatch, urls=["http://www.tidal.com/album/529619006"],
                upcs=["883105707469"])

    async def resolve(url):
        assert "tidal" in url
        return ("album", "42")

    monkeypatch.setattr(upload_import.metadata, "resolve_link", resolve)
    monkeypatch.setattr(upload_import.deezer, "get_album_by_upc", _fail)

    assert await identify_album("/staging", "x.zip") == "42"


async def test_track_url_hops_to_album(monkeypatch):
    set_signals(monkeypatch, urls=["http://www.tidal.com/track/529619009"])

    async def resolve(url):
        return ("track", "777")

    async def get_track(tid):
        assert tid == "777"
        return {"album": {"id": 42}}

    monkeypatch.setattr(upload_import.metadata, "resolve_link", resolve)
    monkeypatch.setattr(upload_import.deezer, "get_track", get_track)

    assert await identify_album("/staging", "x.zip") == "42"


async def test_upc_then_isrc_rungs(monkeypatch):
    set_signals(monkeypatch, upcs=["883105707469"], isrcs=["QT3F42665716"])

    async def upc_fails(upc):
        raise upload_import.deezer.DeezerError("no data")

    async def by_isrc(isrc):
        assert isrc == "QT3F42665716"
        return {"album": {"id": 55}}

    monkeypatch.setattr(upload_import.deezer, "get_album_by_upc", upc_fails)
    monkeypatch.setattr(upload_import.deezer, "get_track_by_isrc", by_isrc)

    assert await identify_album("/staging", "x.zip") == "55"


async def test_artist_album_tags_rung(monkeypatch):
    set_signals(monkeypatch, artist="$wagZilla", album="Can't Hesitate")

    async def find(artist, title):
        assert (artist, title) == ("$wagZilla", "Can't Hesitate")
        return "77"

    monkeypatch.setattr(upload_import.deezer, "find_album_id", find)
    assert await identify_album("/staging", "x.zip") == "77"


async def test_name_fallback_tries_both_orders(monkeypatch):
    set_signals(monkeypatch)

    async def find(artist, title):
        # zip is "Title - Artist"; only the swapped order matches
        return "88" if artist == "$wagZilla" else None

    monkeypatch.setattr(upload_import.deezer, "find_album_id", find)
    assert await identify_album("/staging", "Can't Hesitate - $wagZilla.zip") == "88"


async def test_no_signals_no_id(monkeypatch):
    set_signals(monkeypatch)
    assert await identify_album("/staging", "IMG_2024") is None


# --- signal harvesting ------------------------------------------------------

class FakeTags:
    def __init__(self, d):
        self._d = d

    def keys(self):
        return self._d.keys()

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)


class FakeAudio:
    def __init__(self, tags):
        self.tags = FakeTags(tags)


def test_read_signals_harvests_urls_isrc_upc(tmp_path, monkeypatch):
    (tmp_path / "01 - a.flac").write_bytes(b"x")

    raw = {
        "TIDAL_TRACK_URL": ["http://www.tidal.com/track/529619009"],
        "TIDAL_ALBUM_URL": ["http://www.tidal.com/album/529619006"],
        "ISRC": ["QT3F42665716"],
        "UPC": ["883105707469"],
        "COPYRIGHT": ["Getting Money Gang"],
    }
    easy_tags = {"artist": ["$wagZilla"], "albumartist": [], "album": ["Can't Hesitate"]}

    def fake_mutagen(path, easy=False):
        return FakeAudio(easy_tags) if easy else FakeAudio(raw)

    monkeypatch.setattr(upload_import, "MutagenFile", fake_mutagen)
    sig = _read_signals(str(tmp_path))

    assert sig["urls"][0].endswith("/album/529619006")  # album URL sorts first
    assert sig["isrcs"] == ["QT3F42665716"]
    assert sig["upcs"] == ["883105707469"]
    assert sig["artist"] == "$wagZilla" and sig["album"] == "Can't Hesitate"


def test_stringify_handles_bytes_and_scalars():
    assert _stringify([b"a\xff", "b"]) == ["a", "b"]
    assert _stringify("x") == ["x"]


# --- staged-album import ----------------------------------------------------

ALBUM = {
    "id": "42", "artist": "$wagZilla", "title": "Can't Hesitate",
    "cover_uuid": "", "numberOfVolumes": 1,
    "tracks": [
        {"id": "t1", "title": "Can't Hesitate", "artist": "$wagZilla",
         "trackNumber": 1, "discNumber": 1, "duration": 100},
        {"id": "t2", "title": "Ghost Track", "artist": "$wagZilla",
         "trackNumber": 2, "discNumber": 1, "duration": 90},
    ],
}


@pytest.fixture
def quiet_pipeline(monkeypatch):
    """No-op the network/tagging edges of the import."""
    async def no_cover(_uuid, _dir):
        return None

    async def no_lyrics(*a, **k):
        return {"plain": "la la"}

    monkeypatch.setattr(upload_import, "_download_cover", no_cover)
    monkeypatch.setattr(upload_import.metadata, "fetch_lyrics", no_lyrics)
    monkeypatch.setattr(upload_import, "_write_tags_force",
                        lambda *a, **k: None)


async def test_import_moves_matches_and_reports_missing(tmp_path, quiet_pipeline,
                                                        monkeypatch):
    staging = tmp_path / "staging" / "Album"
    staging.mkdir(parents=True)
    src = staging / "01 - $wagZilla - Can't Hesitate.flac"
    src.write_bytes(b"F" * 10)
    music = tmp_path / "music"
    music.mkdir()

    def staged_match(root, track):
        # only the staged tree has this track; the library lookup misses
        in_staging = str(tmp_path / "staging") in root
        return str(src) if track["id"] == "t1" and in_staging else None

    monkeypatch.setattr(upload_import, "_find_existing_track", staged_match)

    result = await import_staged_album(ALBUM, str(tmp_path / "staging"), str(music))

    dest = music / "$wagZilla" / "Can't Hesitate" / "01 Can't Hesitate.flac"
    assert dest.exists() and not src.exists()
    assert result["downloaded"] == 1 and result["skipped"] == 0
    assert result["failed"] == [("Ghost Track", "missing from the upload")]
    assert result["with_lyrics"] == 1
    assert result["format"].startswith("FLAC")
    assert result["source_counts"] == {"local upload": 1}
    assert result["leftover_files"] == []
    assert not (tmp_path / "staging").exists()  # fully consumed → cleaned up


async def test_import_dedups_against_library(tmp_path, quiet_pipeline, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    src = staging / "01 - dupe.flac"
    src.write_bytes(b"F")
    music = tmp_path / "music"
    lib_file = music / "$wagZilla" / "Can't Hesitate" / "01 Can't Hesitate.flac"
    lib_file.parent.mkdir(parents=True)
    lib_file.write_bytes(b"L")

    def find(root, track):
        if track["id"] != "t1":
            return None
        # library lookup vs staged lookup — answer for both
        return str(lib_file) if str(music) in root else str(src)

    patched = []

    async def patch(path, track, album):
        patched.append(path)
        return []

    monkeypatch.setattr(upload_import, "_find_existing_track", find)
    monkeypatch.setattr(upload_import, "_patch_missing_tags", patch)

    result = await import_staged_album(ALBUM, str(staging), str(music))

    assert result["skipped"] == 1 and result["downloaded"] == 0
    assert not src.exists()          # redundant staged copy dropped
    assert lib_file.read_bytes() == b"L"  # library copy untouched
    assert patched == [str(lib_file)]


async def test_import_keeps_unmatched_leftovers(tmp_path, quiet_pipeline, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "bonus remix.flac").write_bytes(b"F")
    music = tmp_path / "music"
    music.mkdir()

    monkeypatch.setattr(upload_import, "_find_existing_track",
                        lambda root, track: None)

    result = await import_staged_album(ALBUM, str(staging), str(music))

    assert result["downloaded"] == 0 and len(result["failed"]) == 2
    assert result["leftover_files"] == ["bonus remix.flac"]
    assert (staging / "bonus remix.flac").exists()  # kept for inspection
