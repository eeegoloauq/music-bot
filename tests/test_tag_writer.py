"""Shared tag-writing modes preserve the library's curated metadata."""

import shutil
import subprocess
from unittest.mock import Mock

import pytest
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, TEXT, TCOM, TCON, TIT2, TXXX, USLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

import retagger
from library import tagger
from library.files import _comment_value


@pytest.fixture(params=[".flac", ".m4a", ".mp3"])
def tagged_file(tmp_path, request):
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    path = tmp_path / f"track{request.param}"
    cmd = ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
           "anullsrc=r=44100:cl=mono", "-t", "0.2", "-map_metadata", "-1"]
    if request.param == ".m4a":
        cmd += ["-c:a", "aac"]
    subprocess.run(cmd + [str(path)], check=True)
    track = {"title": "Song", "artist": "Artist", "trackNumber": 1,
             "discNumber": 1, "isrc": "TEST123", "track_gain": -2.5}
    album = {"id": "42", "title": "Album", "artist": "Artist",
             "numberOfTracks": 1, "numberOfVolumes": 1,
             "releaseDate": "2026-01-02", "genres": ["Rock"]}
    tagger.write_tags(str(path), track, album, mode="replace")
    return path, track, album


def _seed_curated(path):
    """Leave a stale title alongside metadata that retag must preserve."""
    if path.suffix == ".flac":
        audio = FLAC(path)
        audio["title"] = "Old"
        audio["comment"] = _comment_value("1") + " · Session note · extra"
        audio["genre"] = ["Jazz", "ROCK"]
        audio["lyrics"] = "Curated lyrics"
        audio["composer"] = "Composer"
        audio["custom"] = "Keep me"
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, "image/jpeg", b"artwork"
        audio.add_picture(pic)
        audio.save()
    elif path.suffix == ".m4a":
        audio = MP4(path)
        audio["\xa9nam"] = ["Old"]
        audio["\xa9cmt"] = [_comment_value("1") + " · Session note · extra"]
        audio["\xa9gen"] = ["Jazz; ROCK"]
        audio["\xa9lyr"] = ["Curated lyrics"]
        audio["\xa9wrt"] = ["Composer"]
        audio["----:com.apple.iTunes:CUSTOM"] = [MP4FreeForm(b"Keep me")]
        audio["covr"] = [MP4Cover(b"artwork", imageformat=MP4Cover.FORMAT_JPEG)]
        audio.save()
    else:
        audio = MP3(path)
        tags = audio.tags
        tags.add(TIT2(encoding=3, text="Old"))
        tags.add(COMM(encoding=3, lang="eng", desc="", text=_comment_value("1") + " · Session note · extra"))
        tags.add(TCON(encoding=3, text=["Jazz", "ROCK"]))
        tags.add(USLT(encoding=3, lang="eng", desc="", text="Curated lyrics"))
        tags.add(TCOM(encoding=3, text="Composer"))
        tags.add(TEXT(encoding=3, text="Lyricist"))
        tags.add(TXXX(encoding=3, desc="CUSTOM", text="Keep me"))
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=b"artwork"))
        audio.save(v2_version=4)


def _curated(path):
    if path.suffix == ".flac":
        audio = FLAC(path)
        return (audio["title"][0], audio["comment"][0], audio["genre"],
                audio["lyrics"][0], audio["composer"][0], audio["custom"][0],
                audio.pictures[0].data)
    if path.suffix == ".m4a":
        audio = MP4(path)
        return (audio["\xa9nam"][0], audio["\xa9cmt"][0], audio["\xa9gen"][0].split("; "),
                audio["\xa9lyr"][0], audio["\xa9wrt"][0],
                bytes(audio["----:com.apple.iTunes:CUSTOM"][0]).decode(), bytes(audio["covr"][0]))
    audio = MP3(path).tags
    return (str(audio["TIT2"]), audio.getall("COMM")[0].text[0], list(audio["TCON"].text),
            audio.getall("USLT")[0].text, str(audio["TCOM"]),
            str(audio["TXXX:CUSTOM"]), audio.getall("APIC")[0].data)


def test_retag_preserves_curated_metadata_and_is_idempotent(tagged_file):
    path, track, album = tagged_file
    _seed_curated(path)
    album["genres"] = ["rock", "Electronic", "electronic"]

    assert tagger.write_tags(str(path), track, album, mode="retag")
    assert _curated(path) == (
        "Song", _comment_value("42") + " · Session note · extra",
        ["Jazz", "ROCK", "Electronic"], "Curated lyrics", "Composer", "Keep me", b"artwork",
    )
    before = path.read_bytes()
    assert not tagger.write_tags(str(path), track, album, mode="retag")
    assert path.read_bytes() == before


def test_fill_preserves_existing_title_genres_and_credits(tagged_file):
    path, track, album = tagged_file
    _seed_curated(path)
    before = _curated(path)
    tagger.write_tags(str(path), track, album, mode="fill")
    after = _curated(path)
    # The source marker is deliberately normalized in all writing modes.
    assert after[:1] + after[2:] == before[:1] + before[2:]
    assert after[1] == _comment_value("42")


def test_replace_preserves_curated_credits(tagged_file):
    path, track, album = tagged_file
    _seed_curated(path)
    assert tagger.write_tags(str(path), track, album, mode="replace")
    if path.suffix == ".flac":
        assert FLAC(path)["composer"] == ["Composer"]
        assert "custom" not in FLAC(path)
    elif path.suffix == ".m4a":
        assert MP4(path)["\xa9wrt"] == ["Composer"]
        assert "----:com.apple.iTunes:CUSTOM" not in MP4(path)
    else:
        assert str(MP3(path).tags["TCOM"]) == "Composer"
        assert str(MP3(path).tags["TEXT"]) == "Lyricist"
        assert "TXXX:CUSTOM" not in MP3(path).tags


def test_genre_only_append_is_shared_and_idempotent(tagged_file):
    path, _, _ = tagged_file
    _seed_curated(path)
    before = _curated(path)
    assert tagger.append_genres(str(path), ["rock", "Electronic", "electronic"])
    after = _curated(path)
    assert after[:2] + after[3:] == before[:2] + before[3:]
    assert after[2] == ["Jazz", "ROCK", "Electronic"]
    assert not tagger.append_genres(str(path), ["Electronic"])


@pytest.mark.parametrize("existing", [
    "Peer note", _comment_value("1") + " · Peer note",
    "music-bot · https://www.deezer.com/album/1 · Peer note",
])
def test_comment_migration_keeps_only_peer_note(existing):
    assert tagger._canonical_comment("42", existing, True) == _comment_value("42") + " · Peer note"


def test_retag_orchestration_uses_shared_writer(monkeypatch, tmp_path):
    path = tmp_path / "Artist" / "Album" / "01.flac"
    path.parent.mkdir(parents=True)
    path.touch()
    track = {"title": "Song", "artist": "Artist", "trackNumber": 1}
    album = {"title": "Album", "artist": "Artist", "tracks": [track]}
    plan = retagger.AlbumPlan(folder=str(path.parent), artist_dir="Artist", album_dir="Album",
                             files=[str(path)], fresh_meta=album)
    monkeypatch.setattr(retagger, "_read_file_summary", lambda _: {"tracknumber": 1, "isrc": "", "title": "Song"})
    writer = Mock(return_value=True)
    monkeypatch.setattr(retagger, "write_tags", writer)
    assert retagger._apply_album_sync(plan) == (1, 0)
    writer.assert_called_once_with(str(path), track, album, mode="retag")
