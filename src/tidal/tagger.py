import logging
import os

from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from tidal.files import _comment_value
from tidal.metadata import fetch_lyrics

logger = logging.getLogger(__name__)


def _write_tags(filepath: str, track: dict, album: dict,
                stream_meta: dict | None, cover_data: bytes | None,
                lyrics: dict | None = None):
    """Write Vorbis Comment tags to a FLAC file. Only writes missing tags."""
    try:
        audio = FLAC(filepath)
        artist_str = track.get("artist", album.get("artist", "Unknown"))
        feat = track.get("featuredArtists", [])
        if feat:
            artist_str += " feat. " + ", ".join(feat)

        title_str = track["title"]
        if track.get("version"):
            title_str += f" ({track['version']})"

        disc = track.get("discNumber", 1)
        num = track.get("trackNumber", 0)

        tags = {
            "artist": artist_str,
            "albumartist": album.get("artist", track.get("artist", "Unknown")),
            "album": album.get("title", "Singles"),
            "title": title_str,
            "tracknumber": str(num),
            "discnumber": str(disc),
            "totaltracks": str(album.get("numberOfTracks", 0)),
            "totaldiscs": str(album.get("numberOfVolumes", 1)),
            "date": album.get("releaseDate", ""),
            "copyright": track.get("copyright") or album.get("copyright", ""),
            "isrc": track.get("isrc", ""),
            "barcode": album.get("upc", ""),
        }

        album_id = album.get("id", "")
        if album_id:
            tags["comment"] = _comment_value(album_id)

        if album.get("type"):
            tags["releasetype"] = album["type"].lower()
        if track.get("bpm"):
            tags["bpm"] = str(track["bpm"])
        if track.get("explicit"):
            tags["itunesadvisory"] = "1"

        rg = stream_meta or {}
        if rg.get("trackReplayGain") is not None:
            tags["replaygain_track_gain"] = f"{rg['trackReplayGain']:.2f} dB"
        if rg.get("trackPeak") is not None:
            tags["replaygain_track_peak"] = f"{rg['trackPeak']:.6f}"
        if rg.get("albumReplayGain") is not None:
            tags["replaygain_album_gain"] = f"{rg['albumReplayGain']:.2f} dB"
        if rg.get("albumPeak") is not None:
            tags["replaygain_album_peak"] = f"{rg['albumPeak']:.6f}"

        for key, val in tags.items():
            if val and key not in audio:
                audio[key] = val

        if cover_data and not audio.pictures:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.data = cover_data
            audio.add_picture(pic)

        if lyrics:
            synced = lyrics.get("syncedLyrics")
            plain = lyrics.get("plainLyrics")
            if synced:
                # lyrics = LRC so Navidrome detects timestamps → serves as synced via API
                if "lyrics" not in audio:
                    audio["lyrics"] = synced
                if "syncedlyrics" not in audio:
                    audio["syncedlyrics"] = synced  # for foobar2000, Poweramp, etc.
                if plain and "unsyncedlyrics" not in audio:
                    audio["unsyncedlyrics"] = plain  # fallback for plain-only players
            elif plain:
                if "lyrics" not in audio:
                    audio["lyrics"] = plain
        elif "lrclibchecked" not in audio:
            audio["lrclibchecked"] = "1"

        audio.save()
    except Exception:
        logger.warning("Could not write tags to %s", filepath)


def _write_m4a_tags(filepath: str, track: dict, album: dict,
                    stream_meta: dict | None, cover_data: bytes | None,
                    lyrics: dict | None = None):
    """Write iTunes-style tags to an M4A file. Only writes missing tags."""
    try:
        audio = MP4(filepath)

        artist_str = track.get("artist", album.get("artist", "Unknown"))
        feat = track.get("featuredArtists", [])
        if feat:
            artist_str += " feat. " + ", ".join(feat)

        title_str = track["title"]
        if track.get("version"):
            title_str += f" ({track['version']})"

        disc = track.get("discNumber", 1)
        num = track.get("trackNumber", 0)
        total_tracks = album.get("numberOfTracks", 0)
        total_discs = album.get("numberOfVolumes", 1)

        # Standard iTunes string tags
        str_tags = {
            "\xa9nam": title_str,
            "\xa9ART": artist_str,
            "aART": album.get("artist", track.get("artist", "Unknown")),
            "\xa9alb": album.get("title", "Singles"),
            "\xa9day": album.get("releaseDate", ""),
            "cprt": track.get("copyright") or album.get("copyright", ""),
        }
        album_id = album.get("id", "")
        if album_id:
            str_tags["\xa9cmt"] = _comment_value(album_id)

        for key, val in str_tags.items():
            if val and key not in audio:
                audio[key] = [val]

        if "trkn" not in audio:
            audio["trkn"] = [(num, total_tracks)]
        if "disk" not in audio:
            audio["disk"] = [(disc, total_discs)]
        if track.get("explicit") and "rtng" not in audio:
            audio["rtng"] = [1]

        # Free-form tags (----:com.apple.iTunes:NAME)
        def _ff(name: str, value: str):
            key = f"----:com.apple.iTunes:{name}"
            if key not in audio:
                audio[key] = [MP4FreeForm(value.encode("utf-8"))]

        if track.get("isrc"):
            _ff("ISRC", track["isrc"])
        if album.get("upc"):
            _ff("BARCODE", album["upc"])
        if album.get("type"):
            _ff("RELEASETYPE", album["type"].lower())
        if track.get("bpm"):
            _ff("BPM", str(track["bpm"]))

        rg = stream_meta or {}
        if rg.get("trackReplayGain") is not None:
            _ff("REPLAYGAIN_TRACK_GAIN", f"{rg['trackReplayGain']:.2f} dB")
        if rg.get("trackPeak") is not None:
            _ff("REPLAYGAIN_TRACK_PEAK", f"{rg['trackPeak']:.6f}")
        if rg.get("albumReplayGain") is not None:
            _ff("REPLAYGAIN_ALBUM_GAIN", f"{rg['albumReplayGain']:.2f} dB")
        if rg.get("albumPeak") is not None:
            _ff("REPLAYGAIN_ALBUM_PEAK", f"{rg['albumPeak']:.6f}")

        # Cover art
        if cover_data and "covr" not in audio:
            audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]

        # Lyrics
        if lyrics:
            if lyrics.get("plainLyrics") and "\xa9lyr" not in audio:
                audio["\xa9lyr"] = [lyrics["plainLyrics"]]
            if lyrics.get("syncedLyrics"):
                _ff("SYNCEDLYRICS", lyrics["syncedLyrics"])
        elif "----:com.apple.iTunes:LRCLIBCHECKED" not in audio:
            _ff("LRCLIBCHECKED", "1")

        audio.save()
    except Exception:
        logger.warning("Could not write M4A tags to %s", filepath)


async def _patch_missing_tags(filepath: str, track: dict, album: dict) -> list[str]:
    """Add missing comment and lyrics to an existing file. Returns list of added tag names.

    Skips non-FLAC/non-M4A files.
    Skips lrclib if lyrics already present or lrclibchecked marker is set.
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".flac":
        return await _patch_flac_tags(filepath, track, album)
    elif ext == ".m4a":
        return await _patch_m4a_tags(filepath, track, album)
    return []


async def _patch_flac_tags(filepath: str, track: dict, album: dict) -> list[str]:
    added = []
    try:
        audio = FLAC(filepath)

        album_id = album.get("id", "")
        if album_id and "comment" not in audio:
            audio["comment"] = _comment_value(album_id)
            added.append("comment")

        # Migrate existing files: if lyrics=plain but syncedlyrics=LRC,
        # promote LRC to lyrics so Navidrome serves it as synced via API.
        synced_val = next(iter(audio.get("syncedlyrics") or []), "")
        lyrics_val = next(iter(audio.get("lyrics") or []), "")
        if synced_val and lyrics_val and not lyrics_val.lstrip().startswith("["):
            # lyrics is plain text, syncedlyrics is LRC → fix layout
            if "unsyncedlyrics" not in audio:
                audio["unsyncedlyrics"] = lyrics_val
            audio["lyrics"] = synced_val
            added.append("lyrics→lrc")

        elif "lyrics" not in audio and "syncedlyrics" not in audio and "lrclibchecked" not in audio:
            lyrics = await fetch_lyrics(
                track["title"], track["artist"], album["title"], track["duration"],
            )
            if lyrics:
                synced = lyrics.get("syncedLyrics")
                plain = lyrics.get("plainLyrics")
                if synced:
                    audio["lyrics"] = synced
                    audio["syncedlyrics"] = synced
                    added.append("syncedlyrics")
                    if plain:
                        audio["unsyncedlyrics"] = plain
                        added.append("unsyncedlyrics")
                elif plain:
                    audio["lyrics"] = plain
                    added.append("lyrics")
            else:
                audio["lrclibchecked"] = "1"

        if added:
            audio.save()
    except Exception as e:
        logger.warning("Could not patch FLAC tags for %s: %s", filepath, e)
    return added


async def _patch_m4a_tags(filepath: str, track: dict, album: dict) -> list[str]:
    added = []
    try:
        audio = MP4(filepath)

        album_id = album.get("id", "")
        if album_id and "\xa9cmt" not in audio:
            audio["\xa9cmt"] = [_comment_value(album_id)]
            added.append("comment")

        lrclib_key = "----:com.apple.iTunes:LRCLIBCHECKED"
        has_lyrics = "\xa9lyr" in audio
        has_checked = lrclib_key in audio

        if not has_lyrics and not has_checked:
            lyrics = await fetch_lyrics(
                track["title"], track["artist"], album["title"], track["duration"],
            )
            if lyrics:
                if lyrics.get("plainLyrics"):
                    audio["\xa9lyr"] = [lyrics["plainLyrics"]]
                    added.append("lyrics")
                if lyrics.get("syncedLyrics"):
                    audio["----:com.apple.iTunes:SYNCEDLYRICS"] = [
                        MP4FreeForm(lyrics["syncedLyrics"].encode("utf-8"))
                    ]
                    added.append("syncedlyrics")
            else:
                audio[lrclib_key] = [MP4FreeForm(b"1")]

        if added:
            audio.save()
    except Exception as e:
        logger.warning("Could not patch M4A tags for %s: %s", filepath, e)
    return added
