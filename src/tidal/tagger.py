import logging
import os

from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from tidal.files import _comment_value
from tidal.metadata import fetch_lyrics

logger = logging.getLogger(__name__)


def _format_artist(track: dict, album: dict) -> str:
    """Build display artist string with featured artists."""
    artist_str = track.get("artist", album.get("artist", "Unknown"))
    feat = track.get("featuredArtists", [])
    if feat:
        artist_str += " feat. " + ", ".join(feat)
    return artist_str


def _format_title(track: dict) -> str:
    """Build display title with version suffix."""
    title_str = track["title"]
    if track.get("version"):
        title_str += f" ({track['version']})"
    return title_str


def _write_tags(filepath: str, track: dict, album: dict,
                stream_meta: dict | None, cover_data: bytes | None,
                lyrics: dict | None = None, force: bool = False):
    """Write Vorbis Comment tags to a FLAC file.

    force=False: only fills in missing tags (preserves anything already there).
    force=True : wipes existing tags first, then writes a clean canonical set
                  derived from album/track metadata (Soulseek path, where the
                  peer's tagging is unreliable and inconsistent across formats).
    """
    try:
        audio = FLAC(filepath)
        # Capture a small allow-list of peer tags before the wipe — these are
        # things uploaders sometimes hand-curate that no streaming-service API
        # provides (composer/lyricist/performer credits, free-form comments).
        peer_keep: dict[str, str] = {}
        if force:
            for k in ("comment", "composer", "lyricist", "performer"):
                v = audio.get(k)
                if v:
                    peer_keep[k] = v[0]
            for k in list(audio.keys()):
                del audio[k]
            audio.clear_pictures()
        artist_str = _format_artist(track, album)
        title_str = _format_title(track)

        disc = track.get("discNumber", 1)
        num = track.get("trackNumber", 0)

        release_date = album.get("releaseDate", "")
        release_year = release_date.split("-")[0] if release_date else ""
        tags = {
            "artist": artist_str,
            "albumartist": album.get("artist", track.get("artist", "Unknown")),
            "album": album.get("title", "Singles"),
            "title": title_str,
            "tracknumber": str(num),
            "discnumber": str(disc),
            "totaltracks": str(album.get("numberOfTracks", 0)),
            "totaldiscs": str(album.get("numberOfVolumes", 1)),
            # Navidrome's album persistent-ID hashes both `date` and
            # `releasedate`. If the field is missing on FLAC but present on
            # M4A (Navidrome derives release_date from ©day on M4A), the same
            # album splits in two. Writing both keeps FLAC and M4A grouped.
            "date": release_year,
            "year": release_year,
            "originaldate": release_date,
            "releasedate": release_date,
            "copyright": track.get("copyright") or album.get("copyright", ""),
            "isrc": track.get("isrc", ""),
            "barcode": album.get("upc", ""),
        }
        # Genres (multi-value Vorbis comment)
        genres = album.get("genres") or []
        if genres:
            audio["genre"] = genres
        if album.get("label"):
            tags["publisher"] = album["label"]

        album_id = album.get("id", "")
        if album_id:
            our_comment = _comment_value(album_id)
            peer_comment = peer_keep.get("comment", "").strip()
            # Append peer's hand-written comment after our identifier so the
            # bot still finds its files but the uploader's note ("vinyl rip",
            # "from CD" etc) survives.
            if peer_comment and "music-bot" not in peer_comment.lower():
                tags["comment"] = f"{our_comment} · {peer_comment}"
            else:
                tags["comment"] = our_comment

        if album.get("type"):
            tags["releasetype"] = album["type"].lower()
        if track.get("bpm"):
            tags["bpm"] = str(track["bpm"])
        if track.get("explicit"):
            tags["itunesadvisory"] = "1"

        # ReplayGain from Deezer's gain field. Peak is missing from the API —
        # players that need it will compute on first play. RG2 reference.
        tg = track.get("track_gain")
        if tg is not None:
            tags["replaygain_track_gain"] = f"{tg:+.2f} dB"
        ag = album.get("album_gain")
        if ag is not None:
            tags["replaygain_album_gain"] = f"{ag:+.2f} dB"
        if tg is not None or ag is not None:
            tags["replaygain_reference_loudness"] = "89.0 dB"

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
            if not val:
                continue
            if force or key == "comment" or key not in audio:
                audio[key] = val

        if cover_data:
            if force and audio.pictures:
                audio.clear_pictures()
            if not audio.pictures:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.data = cover_data
                audio.add_picture(pic)

        # Restore peer-curated credits the streaming API never provides.
        for k in ("composer", "lyricist", "performer"):
            val = peer_keep.get(k, "").strip()
            if val:
                audio[k] = val

        if lyrics:
            synced = lyrics.get("syncedLyrics")
            plain = lyrics.get("plainLyrics")
            if synced:
                # lyrics = LRC so Navidrome detects timestamps → serves as synced via API
                if force or "lyrics" not in audio:
                    audio["lyrics"] = synced
                if force or "syncedlyrics" not in audio:
                    audio["syncedlyrics"] = synced  # for foobar2000, Poweramp, etc.
                if plain and (force or "unsyncedlyrics" not in audio):
                    audio["unsyncedlyrics"] = plain  # fallback for plain-only players
            elif plain:
                if force or "lyrics" not in audio:
                    audio["lyrics"] = plain
        elif "lrclibchecked" not in audio:
            audio["lrclibchecked"] = "1"

        audio.save()
    except Exception:
        logger.warning("Could not write tags to %s", filepath, exc_info=True)


def _write_m4a_tags(filepath: str, track: dict, album: dict,
                    stream_meta: dict | None, cover_data: bytes | None,
                    lyrics: dict | None = None, force: bool = False):
    """Write iTunes-style tags to an M4A file.

    force=False: only fills in missing tags.
    force=True : wipes existing tags first, then writes a clean canonical set.
    """
    try:
        audio = MP4(filepath)
        peer_keep: dict[str, str] = {}
        if force:
            # Capture peer-curated credits before the wipe — same reason as FLAC.
            def _decode(v):
                if not v: return ""
                v0 = v[0]
                if isinstance(v0, MP4FreeForm):
                    return bytes(v0).decode("utf-8", errors="ignore")
                return str(v0)
            peer_keep["comment"] = _decode(audio.get("\xa9cmt"))
            peer_keep["composer"] = _decode(audio.get("\xa9wrt"))
            peer_keep["lyricist"] = _decode(audio.get("----:com.apple.iTunes:LYRICIST"))
            peer_keep["performer"] = _decode(audio.get("----:com.apple.iTunes:PERFORMER"))
            for k in list(audio.keys()):
                del audio[k]

        artist_str = _format_artist(track, album)
        title_str = _format_title(track)

        disc = track.get("discNumber", 1)
        num = track.get("trackNumber", 0)
        total_tracks = album.get("numberOfTracks", 0)
        total_discs = album.get("numberOfVolumes", 1)

        release_date = album.get("releaseDate", "")
        # Standard iTunes string tags
        str_tags = {
            "\xa9nam": title_str,
            "\xa9ART": artist_str,
            "aART": album.get("artist", track.get("artist", "Unknown")),
            "\xa9alb": album.get("title", "Singles"),
            "\xa9day": release_date,
            "cprt": track.get("copyright") or album.get("copyright", ""),
        }
        album_id = album.get("id", "")
        if album_id:
            our_comment = _comment_value(album_id)
            peer_comment = peer_keep.get("comment", "").strip()
            if peer_comment and "music-bot" not in peer_comment.lower():
                str_tags["\xa9cmt"] = f"{our_comment} · {peer_comment}"
            else:
                str_tags["\xa9cmt"] = our_comment

        for key, val in str_tags.items():
            if not val:
                continue
            if force or key == "\xa9cmt" or key not in audio:
                audio[key] = [val]

        if force or "trkn" not in audio:
            audio["trkn"] = [(num, total_tracks)]
        if force or "disk" not in audio:
            audio["disk"] = [(disc, total_discs)]
        if track.get("explicit") and (force or "rtng" not in audio):
            audio["rtng"] = [1]

        # Free-form tags (----:com.apple.iTunes:NAME)
        def _ff(name: str, value: str):
            key = f"----:com.apple.iTunes:{name}"
            if force or key not in audio:
                audio[key] = [MP4FreeForm(value.encode("utf-8"))]

        if track.get("isrc"):
            _ff("ISRC", track["isrc"])
        if album.get("upc"):
            _ff("BARCODE", album["upc"])
        if album.get("type"):
            _ff("RELEASETYPE", album["type"].lower())
        if track.get("bpm"):
            _ff("BPM", str(track["bpm"]))
        if album.get("label"):
            _ff("LABEL", album["label"])
        # Genre — iTunes ©gen takes a single string; join Deezer's list.
        genres = album.get("genres") or []
        if genres and (force or "\xa9gen" not in audio):
            audio["\xa9gen"] = ["; ".join(genres)]

        # ReplayGain from Deezer (track) + computed album-mean.
        tg = track.get("track_gain")
        if tg is not None:
            _ff("REPLAYGAIN_TRACK_GAIN", f"{tg:+.2f} dB")
        ag = album.get("album_gain")
        if ag is not None:
            _ff("REPLAYGAIN_ALBUM_GAIN", f"{ag:+.2f} dB")
        if tg is not None or ag is not None:
            _ff("REPLAYGAIN_REFERENCE_LOUDNESS", "89.0 dB")

        # Cover art
        if cover_data and (force or "covr" not in audio):
            audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]

        # Lyrics
        if lyrics:
            if lyrics.get("plainLyrics") and (force or "\xa9lyr" not in audio):
                audio["\xa9lyr"] = [lyrics["plainLyrics"]]
            if lyrics.get("syncedLyrics"):
                _ff("SYNCEDLYRICS", lyrics["syncedLyrics"])
        elif "----:com.apple.iTunes:LRCLIBCHECKED" not in audio:
            _ff("LRCLIBCHECKED", "1")

        # Restore peer-curated credits.
        if peer_keep.get("composer"):
            audio["\xa9wrt"] = [peer_keep["composer"]]
        if peer_keep.get("lyricist"):
            _ff("LYRICIST", peer_keep["lyricist"])
        if peer_keep.get("performer"):
            _ff("PERFORMER", peer_keep["performer"])

        audio.save()
    except Exception:
        logger.warning("Could not write M4A tags to %s", filepath, exc_info=True)


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
        if album_id:
            new_comment = _comment_value(album_id)
            if audio.get("comment") != [new_comment]:
                audio["comment"] = new_comment
                added.append("comment")

        # Normalize album/albumartist/artist casing+spelling against current
        # metadata. Otherwise old "MIRY" tagged files coexist with new "Miry"
        # ones and Navidrome treats them as separate albums.
        canonical = {
            "album": album.get("title"),
            "albumartist": album.get("artist"),
            "artist": _format_artist(track, album),
        }
        for k, want in canonical.items():
            if not want:
                continue
            if audio.get(k) != [want]:
                audio[k] = want
                added.append(k)

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
        if album_id:
            new_comment = _comment_value(album_id)
            if audio.get("\xa9cmt") != [new_comment]:
                audio["\xa9cmt"] = [new_comment]
                added.append("comment")

        # Normalize album/albumartist/artist casing — same reason as FLAC patch.
        canonical = {
            "\xa9alb": album.get("title"),
            "aART": album.get("artist"),
            "\xa9ART": _format_artist(track, album),
        }
        for k, want in canonical.items():
            if not want:
                continue
            if audio.get(k) != [want]:
                audio[k] = [want]
                added.append({"\xa9alb": "album", "aART": "albumartist",
                              "\xa9ART": "artist"}.get(k, k))

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
