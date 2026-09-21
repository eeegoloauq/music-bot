"""Tag & file a staged local upload — the upload twin of download_album.

The audio is already on disk (``uploads.py`` staged it under
``/data/uploads/.extracted/<id>/``); this module figures out *which release*
it is, then runs the exact downstream path a Soulseek download takes:
canonical Deezer metadata, force-retag, canonical filenames, tag-based
dedup, into the library.

Identification ladder (docs/local-upload-plan.md), first rung that
resolves wins:
  1. a streaming-service URL baked into the tags (e.g. ``TIDAL_ALBUM_URL``)
     → the same ``metadata.resolve_link`` a pasted chat link goes through
  2. ISRC / UPC tags → direct Deezer lookup
  3. artist + album text tags → Deezer search
  4. the artist the audio *file names* agree on (``NN. Artist - Title``)
     + the zip/folder name as the album title
  5. the zip/folder name, tried as both "Artist - Title" and "Title - Artist"

Rung 4 sits before the bare name because it rests on N files agreeing, not
one free-form string: it settles which side of the name is the artist, and
still works when the name carries no artist at all ("I Hate Rap (2026).zip").
A miss at every rung is logged at WARNING with everything that was tried;
``describe_identify_failure`` turns the same record into the chat reply.
"""

import asyncio
import contextlib
import logging
import os
import re
import shutil
import time

from mutagen import File as MutagenFile

import metadata
from metadata import deezer
from library.files import (
    _ensure_album_dir, _find_existing_track, _locate_existing_album,
    _sanitize, _track_prefix,
)
from library.tagger import _patch_missing_tags
from soulseek.downloader import (
    _download_cover, _move_into_library, _result_dict, _write_tags_force,
    run_to_completion,
)
from uploads import AUDIO_EXTS

logger = logging.getLogger(__name__)

# Services resolve_link understands; anything else in the tags is noise.
_LINK_RE = re.compile(
    r"https?://[^\s'\"]*(?:tidal\.com|deezer\.com|open\.spotify\.com"
    r"|music\.apple\.com|song\.link)[^\s'\"]*",
    re.IGNORECASE,
)
_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
# Track file names as rippers write them: "01. Artist - Title",
# "01 - Artist - Title", "01 Artist - Title", "Artist - Title". The number
# is optional and needs a separator or a leading zero — "50 Cent - Title"
# and "2 Chainz - Title" are artists, not track numbers. The first " - "
# splits artist from title.
_TRACK_NAME_RE = re.compile(
    r"^(?:(?:\d{1,3}\s*[.\-)]|0\d)\s+)?(?P<artist>.+?)\s+-\s+(?P<title>.+)$")


def _stringify(val) -> list[str]:
    """Flatten one mutagen tag value (str / bytes / Frame / list thereof)."""
    out = []
    for v in (val if isinstance(val, (list, tuple)) else [val]):
        if isinstance(v, bytes):
            out.append(v.decode("utf-8", "ignore"))
        else:
            out.append(str(v))
    return out


def _read_signals(staging_dir: str) -> dict:
    """Harvest identify signals from every staged audio file's tags:
    streaming URLs, ISRCs, UPCs, and the first artist/album text pair.
    Album URLs sort before track URLs — one resolver hop instead of two."""
    urls: dict[str, None] = {}
    isrcs: dict[str, None] = {}
    upcs: dict[str, None] = {}
    artist = album = ""
    for root, _dirs, files in os.walk(staging_dir):
        for fname in sorted(files):
            if os.path.splitext(fname)[1].lower() not in AUDIO_EXTS:
                continue
            path = os.path.join(root, fname)
            try:
                raw = MutagenFile(path)
            except Exception:
                continue
            if raw is None or not raw.tags:
                continue
            for key in raw.tags.keys():
                kl = str(key).lower()
                for s in _stringify(raw.tags[key]):
                    for u in _LINK_RE.findall(s):
                        urls[u] = None
                    if "isrc" in kl:
                        c = s.strip().upper().replace("-", "")
                        if _ISRC_RE.match(c):
                            isrcs[c] = None
                    if "upc" in kl or "barcode" in kl:
                        c = re.sub(r"\D", "", s)
                        if 11 <= len(c) <= 14:
                            upcs[c] = None
            if not (artist and album):
                try:
                    ez = MutagenFile(path, easy=True)
                    if ez is not None and ez.tags is not None:
                        artist = artist or next(iter(ez.tags.get("albumartist") or []), "") \
                            or next(iter(ez.tags.get("artist") or []), "")
                        album = album or next(iter(ez.tags.get("album") or []), "")
                except Exception:
                    pass
    return {
        "urls": sorted(urls, key=lambda u: "/album/" not in u),
        "isrcs": list(isrcs),
        "upcs": list(upcs),
        "artist": str(artist),
        "album": str(album),
    }


def _consensus_artist(staging_dir: str) -> str | None:
    """The artist at least half of the staged audio file names name in an
    ``[NN.] Artist - Title`` pattern, or None. Untagged rips usually still
    carry the artist in every file name."""
    votes: dict[str, str] = {}
    counts: dict[str, int] = {}
    n_files = 0
    for _root, _dirs, files in os.walk(staging_dir):
        for fname in sorted(files):  # first spelling seen wins the casing
            stem, ext = os.path.splitext(fname)
            if ext.lower() not in AUDIO_EXTS:
                continue
            n_files += 1
            m = _TRACK_NAME_RE.match(stem.strip())
            if not m:
                continue
            artist = m.group("artist").strip()
            key = artist.casefold()
            votes.setdefault(key, artist)
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    key, n = max(counts.items(), key=lambda kv: kv[1])
    if n * 2 < n_files or sum(1 for c in counts.values() if c == n) > 1:
        return None  # under half, or a tie — no consensus
    return votes[key]


def _strip_zip(fallback_name: str) -> str:
    return re.sub(r"\.zip$", "", fallback_name, flags=re.IGNORECASE).strip()


def _title_from_name(name: str, artist: str) -> str:
    """Album title from a zip/folder name once the artist is known: drop the
    side of ``Artist - Title`` that is the artist, else keep the whole name
    (the year is stripped by deezer._clean_title on search)."""
    if " - " in name:
        left, _, right = name.partition(" - ")
        if left.strip().casefold() == artist.casefold():
            return right.strip()
        if right.strip().casefold() == artist.casefold():
            return left.strip()
    return name


def describe_identify_failure(diag: dict) -> str:
    """One honest clause for the chat reply: which signals the files had and
    what went to Deezer. ``diag`` is the dict ``identify_album`` fills."""
    sig = diag.get("signals") or {}
    have = [label for key, label in (("urls", "URL"), ("upcs", "UPC"),
                                     ("isrcs", "ISRC"), ("artist", "artist"),
                                     ("album", "album"))
            if sig.get(key)]
    parts = ["tags: " + ("+".join(have) if have else "none")]
    tag_q = diag.get("tag_queries") or []
    if tag_q:
        parts.append("tag search " + ", ".join(f'"{a} — {t}"' for a, t in tag_q))
    name = diag.get("name")
    name_q = diag.get("name_queries") or []
    if name and name_q:
        parts.append(f'name search "{name}"')
    elif name:
        parts.append(f'name "{name}" has no "Artist - Title" shape')
    searched = bool(tag_q or name_q or sig.get("urls") or sig.get("upcs")
                    or sig.get("isrcs"))
    return "; ".join(parts) + (" — no Deezer hit" if searched
                               else " — nothing to search Deezer with")


async def _album_of_track(track_id: str) -> str | None:
    with contextlib.suppress(Exception):
        t = await deezer.get_track(track_id)
        aid = (t.get("album") or {}).get("id")
        if aid:
            return str(aid)
    return None


async def identify_album(staging_dir: str, fallback_name: str,
                         diag: dict | None = None) -> str | None:
    """Walk the ladder; return a Deezer album id or None.

    ``diag`` (optional) collects what was tried for the failure report:
    ``signals`` (which tag signals the files had), ``tag_queries`` and
    ``name_queries`` (artist/title pairs sent to Deezer), ``file_artist``
    (the file-name consensus) and ``name`` (the zip/folder name searched)."""
    diag = diag if diag is not None else {}
    sig = await asyncio.to_thread(_read_signals, staging_dir)
    diag["signals"] = {k: bool(v) for k, v in sig.items()}
    diag["tag_queries"] = []
    diag["name_queries"] = []

    for url in sig["urls"]:
        result = None
        with contextlib.suppress(Exception):
            result = await metadata.resolve_link(url)
        if result:
            typ, rid = result
            aid = rid if typ == "album" else await _album_of_track(rid)
            if aid:
                logger.info("Upload identified via tag URL %s → album %s", url, aid)
                return aid

    for upc in sig["upcs"]:
        with contextlib.suppress(Exception):
            a = await deezer.get_album_by_upc(upc)
            if a.get("id"):
                logger.info("Upload identified via UPC %s → album %s", upc, a["id"])
                return str(a["id"])

    for isrc in sig["isrcs"]:
        with contextlib.suppress(Exception):
            t = await deezer.get_track_by_isrc(isrc)
            aid = (t.get("album") or {}).get("id")
            if aid:
                logger.info("Upload identified via ISRC %s → album %s", isrc, aid)
                return str(aid)

    if sig["artist"] and sig["album"]:
        diag["tag_queries"].append((sig["artist"], sig["album"]))
        with contextlib.suppress(Exception):
            aid = await deezer.find_album_id(sig["artist"], sig["album"])
            if aid:
                logger.info("Upload identified via artist/album tags → %s", aid)
                return aid

    # Untagged rips: the file names still agree on an artist, and the
    # zip/folder name is the album.
    name = _strip_zip(fallback_name)
    diag["name"] = name
    file_artist = await asyncio.to_thread(_consensus_artist, staging_dir)
    diag["file_artist"] = file_artist
    if file_artist and name:
        title = _title_from_name(name, file_artist)
        diag["name_queries"].append((file_artist, title))
        with contextlib.suppress(Exception):
            aid = await deezer.find_album_id(file_artist, title)
            if aid:
                logger.info("Upload identified via file names %r + name %r → album %s",
                            file_artist, name, aid)
                return aid

    # Last resort: the zip/folder name. Both orders — the wild carries
    # "Artist - Title" and "Title - Artist" about equally often.
    if " - " in name:
        left, _, right = name.partition(" - ")
        for artist, title in ((left.strip(), right.strip()),
                              (right.strip(), left.strip())):
            diag["name_queries"].append((artist, title))
            with contextlib.suppress(Exception):
                aid = await deezer.find_album_id(artist, title)
                if aid:
                    logger.info("Upload identified via name %r → album %s", name, aid)
                    return aid

    logger.warning(
        "Upload identify failed for %r: signals urls=%s upcs=%s isrcs=%s artist=%r "
        "album=%r; file-name artist=%r; tag queries=%s; name queries=%s",
        fallback_name, sig["urls"][:5], sig["upcs"][:5], sig["isrcs"][:5],
        sig["artist"], sig["album"], file_artist, diag["tag_queries"],
        diag["name_queries"],
    )
    return None


def _staged_audio(staging_dir: str) -> list[str]:
    return sorted(
        os.path.join(root, f)
        for root, _dirs, files in os.walk(staging_dir)
        for f in files
        if os.path.splitext(f)[1].lower() in AUDIO_EXTS
    )


def _find_staged_track(staging_dir: str, track: dict) -> str | None:
    """Match one Deezer track to a staged file — same ISRC / number+title /
    title matching a library folder gets, applied to each staged subdir."""
    for root, _dirs, files in os.walk(staging_dir):
        if not files:
            continue
        hit = _find_existing_track(root, track)
        if hit:
            return hit
    return None


def _format_label(path: str) -> str:
    ext = os.path.splitext(path)[1].lstrip(".").upper()
    parts = [ext]
    with contextlib.suppress(Exception):
        info = MutagenFile(path).info
        bd = getattr(info, "bits_per_sample", None)
        sr = getattr(info, "sample_rate", None)
        if bd:
            parts.append(f"{bd}-bit")
        if sr:
            parts.append(f"{sr // 1000}kHz")
    return " ".join(parts)


def _file_track(src: str, dest: str, track: dict, album: dict,
                cover_data: bytes | None, lyrics: dict | None, ext: str,
                progress: dict) -> None:
    """Move one staged file into the library and tag it — one worker-thread
    step, so a cancel (docs/cancel.md) sees either the staged file or the
    finished library file, never a moved-but-untagged one. The saved count
    is bumped here, by the thread, so it stays right even when the awaiting
    coroutine was cancelled meanwhile."""
    _move_into_library(src, dest)
    if ext in ("flac", "m4a", "mp3"):
        _write_tags_force(dest, track, album, cover_data, lyrics, ext)
    progress["saved"] = progress.get("saved", 0) + 1


async def import_staged_album(album: dict, staging_dir: str, dest_dir: str,
                              progress: dict | None = None) -> dict:
    """File a staged upload into the library. Returns the download_album
    result-dict shape (reporting.render_album_final renders it), plus
    ``leftover_files``: staged audio that matched no track and stayed put.

    ``progress`` (optional dict) gets ``saved`` — files that reached the
    library — updated as the import goes, for the cancel report."""
    t0 = time.monotonic()
    progress = progress if progress is not None else {}
    progress.setdefault("saved", 0)
    existing_dir = await asyncio.to_thread(_locate_existing_album, dest_dir, album)
    album_dir = existing_dir or _ensure_album_dir(
        dest_dir, _sanitize(album["artist"]), _sanitize(album["title"]))
    cover_data = await _download_cover(album.get("cover_uuid", ""), album_dir)

    skipped = with_lyrics = 0
    failed: list[tuple[str, str]] = []
    format_label = ""
    total = len(album["tracks"])

    for i, track in enumerate(album["tracks"], 1):
        src = await asyncio.to_thread(_find_staged_track, staging_dir, track)
        existing = await asyncio.to_thread(_find_existing_track, album_dir, track)
        if existing:
            # Same dedup semantics as a re-pasted link: keep the library copy,
            # top up missing tags, drop the redundant staged file.
            with contextlib.suppress(Exception):
                await _patch_missing_tags(existing, track, album)
            if src:
                with contextlib.suppress(OSError):
                    os.remove(src)
            skipped += 1
            logger.info("  [%d/%d] %s — already in library, skipping",
                        i, total, track["title"])
            continue
        if not src:
            failed.append((track["title"], "missing from the upload"))
            continue

        ext = os.path.splitext(src)[1].lower().lstrip(".")
        prefix = _track_prefix(track.get("discNumber", 1), track.get("trackNumber", 0),
                               album.get("numberOfVolumes", 1))
        dest = os.path.join(album_dir, f"{prefix} {_sanitize(track['title'])}.{ext}")
        if not format_label:
            format_label = _format_label(src)

        lyrics = None
        with contextlib.suppress(Exception):
            lyrics = await metadata.fetch_lyrics(
                track["title"], track["artist"], album["title"],
                track.get("duration", 0))

        await run_to_completion(asyncio.to_thread(
            _file_track, src, dest, track, album, cover_data, lyrics, ext, progress))
        if lyrics:
            with_lyrics += 1
        logger.info("  [%d/%d] %s — filed from upload [%s]",
                    i, total, track["title"], format_label)

    downloaded = progress["saved"]
    leftovers = await asyncio.to_thread(_staged_audio, staging_dir)
    if not leftovers:
        # nothing worth keeping — art/playlists go with the staging dir
        await asyncio.to_thread(shutil.rmtree, staging_dir, ignore_errors=True)

    result = _result_dict(
        album_dir, downloaded, skipped, failed, total, format_label, with_lyrics,
        elapsed_secs=time.monotonic() - t0,
        source_counts={"local upload": downloaded} if downloaded else None,
    )
    result["leftover_files"] = [os.path.relpath(p, staging_dir) for p in leftovers]
    return result
