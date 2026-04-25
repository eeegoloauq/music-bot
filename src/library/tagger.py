import logging
import os

from mutagen.flac import FLAC, Picture
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.mp3 import MP3
from mutagen.id3 import (
    APIC, COMM, TALB, TBPM, TCON, TCOP, TCOM, TDOR, TDRC, TDRL,
    TIT2, TPE1, TPE2, TPOS, TPUB, TRCK, TSRC, TXXX, USLT,
)

from library.files import _comment_value
from metadata import fetch_lyrics

logger = logging.getLogger(__name__)


def _id3_get_text(audio, frame_id: str) -> str:
    f = audio.get(frame_id)
    if f and f.text:
        return f.text[0] if isinstance(f.text, list) else str(f.text)
    return ""


def _id3_get_txxx(audio, desc: str) -> str:
    for f in audio.getall("TXXX"):
        if f.desc == desc:
            return f.text[0] if f.text else ""
    return ""


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


def _write_mp3_tags(filepath: str, track: dict, album: dict,
                    stream_meta: dict | None, cover_data: bytes | None,
                    lyrics: dict | None = None, force: bool = False):
    """Write ID3v2.4 tags to an mp3 file.

    Used by the mp3 lossy-fallback path. Same force-mode contract as the FLAC /
    M4A writers: capture peer-curated credits before wiping, write canonical
    Deezer-derived set, then restore the allow-list.
    """
    try:
        mp3 = MP3(filepath)
        if mp3.tags is None:
            mp3.add_tags()
        audio = mp3.tags

        peer_keep: dict[str, str] = {}
        if force:
            comm_frames = audio.getall("COMM")
            peer_keep["comment"] = (
                comm_frames[0].text[0] if (comm_frames and comm_frames[0].text) else ""
            )
            peer_keep["composer"] = _id3_get_text(audio, "TCOM")
            peer_keep["lyricist"] = _id3_get_text(audio, "TEXT")
            peer_keep["performer"] = _id3_get_txxx(audio, "PERFORMER")
            audio.clear()

        artist_str = _format_artist(track, album)
        title_str = _format_title(track)

        disc = track.get("discNumber", 1)
        num = track.get("trackNumber", 0)
        total_tracks = album.get("numberOfTracks", 0)
        total_discs = album.get("numberOfVolumes", 1)

        release_date = album.get("releaseDate", "")

        audio.add(TIT2(encoding=3, text=title_str))
        audio.add(TPE1(encoding=3, text=artist_str))
        audio.add(TPE2(encoding=3, text=album.get("artist", track.get("artist", "Unknown"))))
        audio.add(TALB(encoding=3, text=album.get("title", "Singles")))
        audio.add(TRCK(encoding=3, text=f"{num}/{total_tracks}" if total_tracks else str(num)))
        audio.add(TPOS(encoding=3, text=f"{disc}/{total_discs}" if total_discs else str(disc)))
        if release_date:
            # ID3v2.4: TDRC supersedes TYER. TDOR + TDRL track original /
            # release dates the same way FLAC's ORIGINALDATE / RELEASEDATE do.
            audio.add(TDRC(encoding=3, text=release_date))
            audio.add(TDOR(encoding=3, text=release_date))
            audio.add(TDRL(encoding=3, text=release_date))

        cprt = track.get("copyright") or album.get("copyright", "")
        if cprt:
            audio.add(TCOP(encoding=3, text=cprt))
        if track.get("isrc"):
            audio.add(TSRC(encoding=3, text=track["isrc"]))
        if album.get("label"):
            audio.add(TPUB(encoding=3, text=album["label"]))
        if track.get("bpm"):
            audio.add(TBPM(encoding=3, text=str(track["bpm"])))
        genres = album.get("genres") or []
        if genres:
            # mutagen accepts a list; stored as null-separated in v2.4.
            audio.add(TCON(encoding=3, text=genres))

        if album.get("upc"):
            audio.add(TXXX(encoding=3, desc="BARCODE", text=album["upc"]))
        if album.get("type"):
            audio.add(TXXX(encoding=3, desc="RELEASETYPE", text=album["type"].lower()))
        if track.get("explicit"):
            audio.add(TXXX(encoding=3, desc="ITUNESADVISORY", text="1"))

        # ReplayGain via TXXX (foobar2000 / Mp3gain / RG-aware players).
        tg = track.get("track_gain")
        ag = album.get("album_gain")
        if tg is not None:
            audio.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=f"{tg:+.2f} dB"))
        if ag is not None:
            audio.add(TXXX(encoding=3, desc="REPLAYGAIN_ALBUM_GAIN", text=f"{ag:+.2f} dB"))
        if tg is not None or ag is not None:
            audio.add(TXXX(encoding=3, desc="REPLAYGAIN_REFERENCE_LOUDNESS", text="89.0 dB"))

        album_id = album.get("id", "")
        if album_id:
            our_comment = _comment_value(album_id)
            peer_comment = peer_keep.get("comment", "").strip()
            if peer_comment and "music-bot" not in peer_comment.lower():
                comment_text = f"{our_comment} · {peer_comment}"
            else:
                comment_text = our_comment
            audio.add(COMM(encoding=3, lang="eng", desc="", text=comment_text))

        if cover_data:
            audio.add(APIC(
                encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data,
            ))

        if peer_keep.get("composer"):
            audio.add(TCOM(encoding=3, text=peer_keep["composer"]))
        if peer_keep.get("lyricist"):
            audio.add(TXXX(encoding=3, desc="LYRICIST", text=peer_keep["lyricist"]))
        if peer_keep.get("performer"):
            audio.add(TXXX(encoding=3, desc="PERFORMER", text=peer_keep["performer"]))

        if lyrics:
            synced = lyrics.get("syncedLyrics")
            plain = lyrics.get("plainLyrics")
            if synced:
                audio.add(USLT(encoding=3, lang="eng", desc="", text=plain or synced))
                audio.add(TXXX(encoding=3, desc="SYNCEDLYRICS", text=synced))
            elif plain:
                audio.add(USLT(encoding=3, lang="eng", desc="", text=plain))
        else:
            already = any(f.desc == "LRCLIBCHECKED" for f in audio.getall("TXXX"))
            if not already:
                audio.add(TXXX(encoding=3, desc="LRCLIBCHECKED", text="1"))

        mp3.save(v2_version=4)
    except Exception:
        logger.warning("Could not write MP3 tags to %s", filepath, exc_info=True)


async def _patch_missing_tags(filepath: str, track: dict, album: dict) -> list[str]:
    """Add missing comment and lyrics to an existing file. Returns list of added tag names.

    Skips lrclib if lyrics already present or ``lrclibchecked`` marker is set.
    """
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".flac":
        return await _patch_flac_tags(filepath, track, album)
    elif ext == ".m4a":
        return await _patch_m4a_tags(filepath, track, album)
    elif ext == ".mp3":
        return await _patch_mp3_tags(filepath, track, album)
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


async def _patch_mp3_tags(filepath: str, track: dict, album: dict) -> list[str]:
    added = []
    try:
        mp3 = MP3(filepath)
        if mp3.tags is None:
            mp3.add_tags()
        audio = mp3.tags

        album_id = album.get("id", "")
        if album_id:
            new_comment = _comment_value(album_id)
            existing_comm = audio.getall("COMM")
            existing_text = (existing_comm[0].text[0]
                             if existing_comm and existing_comm[0].text else "")
            if existing_text != new_comment:
                # Wipe stale COMM frames; add canonical one
                for f in list(existing_comm):
                    audio.delall(f.HashKey)
                audio.add(COMM(encoding=3, lang="eng", desc="", text=new_comment))
                added.append("comment")

        canonical = {
            "TALB": (TALB, album.get("title")),
            "TPE2": (TPE2, album.get("artist")),
            "TPE1": (TPE1, _format_artist(track, album)),
        }
        for fid, (cls, want) in canonical.items():
            if not want:
                continue
            current = _id3_get_text(audio, fid)
            if current != want:
                audio.add(cls(encoding=3, text=want))
                added.append({"TALB": "album", "TPE2": "albumartist",
                              "TPE1": "artist"}[fid])

        has_lyrics = bool(audio.getall("USLT"))
        has_checked = any(f.desc == "LRCLIBCHECKED" for f in audio.getall("TXXX"))

        if not has_lyrics and not has_checked:
            lyrics = await fetch_lyrics(
                track["title"], track["artist"], album["title"], track["duration"],
            )
            if lyrics:
                synced = lyrics.get("syncedLyrics")
                plain = lyrics.get("plainLyrics")
                if synced:
                    audio.add(USLT(encoding=3, lang="eng", desc="", text=plain or synced))
                    audio.add(TXXX(encoding=3, desc="SYNCEDLYRICS", text=synced))
                    added.append("syncedlyrics")
                elif plain:
                    audio.add(USLT(encoding=3, lang="eng", desc="", text=plain))
                    added.append("lyrics")
            else:
                audio.add(TXXX(encoding=3, desc="LRCLIBCHECKED", text="1"))

        if added:
            mp3.save(v2_version=4)
    except Exception as e:
        logger.warning("Could not patch MP3 tags for %s: %s", filepath, e)
    return added
