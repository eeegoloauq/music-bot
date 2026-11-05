"""Tag & file a staged local upload — the upload twin of download_album.

The audio is already on disk (``uploads.py`` staged it under
``/data/uploads/.extracted/<id>/``); this module figures out *which release*
it is, then runs the exact downstream path a Soulseek download takes:
The owner can keep supplied files unchanged, identify a release, or select
one manually. Release mode applies canonical Deezer tags and filenames with
tag-based dedup, preserving supplied artwork, lyrics and useful sidecars.

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
import hashlib
import json
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
from library.importing import (
    _download_cover, _move_into_library, _result_dict, write_import_tags,
    run_to_completion,
)
from uploads import AUDIO_EXTS, ART_EXTS, SIDECAR_EXTS, IntakeReport, _inventory_tree

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
    if ext in ("flac", "m4a", "mp3"):
        write_import_tags(src, track, album, cover_data, lyrics, ext)
    _move_into_library(src, dest)
    progress["saved"] = progress.get("saved", 0) + 1


def _preserve_extras(staging_dir: str, album_dir: str) -> None:
    """Copy supplied art and sidecars before staging can be removed."""
    files = [os.path.join(root, f) for root, _dirs, names in os.walk(staging_dir)
             for f in names if not f.startswith(".")
             and os.path.splitext(f)[1].lower() in ART_EXTS | SIDECAR_EXTS]
    audio = _staged_audio(staging_dir)
    if not files:
        return
    common = os.path.commonpath([os.path.dirname(f) for f in files + audio])
    for src in files:
        dest = os.path.join(album_dir, os.path.relpath(src, common))
        if not os.path.realpath(dest).startswith(os.path.realpath(album_dir) + os.sep):
            raise ValueError("Sidecar destination escapes the album directory")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # Preserve both copies when a library sidecar already has this name.
        if os.path.exists(dest):
            with open(src, "rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()[:12]
            stem, ext = os.path.splitext(dest)
            dest = f"{stem}-{digest}{ext}"
        if not os.path.exists(dest):
            with open(src, "rb") as source, open(dest, "xb") as target:
                shutil.copyfileobj(source, target)


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
    await run_to_completion(asyncio.to_thread(_preserve_extras, staging_dir, album_dir))
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
        # Keep unsupported extras recoverable; supported sidecars were copied.
        remaining = [os.path.join(root, f)
                     for root, _dirs, files in os.walk(staging_dir) for f in files
                     if not f.startswith(".")
                     and os.path.splitext(f)[1].lower() not in ART_EXTS | SIDECAR_EXTS]
        if not remaining:
            await asyncio.to_thread(shutil.rmtree, staging_dir, ignore_errors=True)

    result = _result_dict(
        album_dir, downloaded, skipped, failed, total, format_label, with_lyrics,
        elapsed_secs=time.monotonic() - t0,
        source_counts={"local upload": downloaded} if downloaded else None,
    )
    result["leftover_files"] = [os.path.relpath(p, staging_dir) for p in leftovers]
    return result


def describe_album(report: IntakeReport) -> dict:
    """Use supplied album tags for placement, with a filename fallback."""
    artists, albums = [], set()
    for rel in report.audio:
        try:
            audio = MutagenFile(os.path.join(report.staging_dir, rel), easy=True)
            tags = audio.tags if audio is not None else None
            if tags:
                artist = next(iter(tags.get("albumartist") or tags.get("artist") or []), "")
                album = next(iter(tags.get("album") or []), "")
                if artist:
                    artists.append(str(artist))
                if album:
                    albums.add(str(album))
        except Exception:
            continue
    if len({a.casefold() for a in albums}) > 1:
        raise ValueError("Upload contains several albums; supply one album at a time")
    name = os.path.splitext(report.name)[0] if report.name.lower().endswith(".zip") else report.name
    artist = "Unknown Artist"
    if artists:
        artist = artists[0] if len({a.casefold() for a in artists}) == 1 else "Various Artists"
    return {"artist": artist, "title": next(iter(sorted(albums)), name) or "Untitled Album"}


def _new_album_dir(dest_dir: str, album: dict) -> str:
    """Reserve a separate directory; an import never overwrites another edition."""
    root = os.path.realpath(dest_dir)
    artist = os.path.join(root, _sanitize(album["artist"]) or "Unknown Artist")
    if not os.path.realpath(artist).startswith(root + os.sep):
        raise ValueError("Artist directory escapes the library")
    os.makedirs(artist, exist_ok=True)
    title = _sanitize(album["title"]) or "Untitled Album"
    for index in range(1, 10000):
        target = os.path.join(artist, title if index == 1 else f"{title} ({index})")
        try:
            os.mkdir(target)
            return target
        except FileExistsError:
            continue
    raise ValueError("Too many copies of this album")


def _file_original(src: str, dest: str, progress: dict, audio: bool) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if audio:
        if os.path.exists(dest):
            raise FileExistsError(f"Import destination already exists: {dest}")
        _move_into_library(src, dest)
        progress["saved"] += 1
    else:
        # Keep sidecars in staging too, so a cancelled import can resume.
        temporary = dest + ".importing"
        with open(src, "rb") as source, open(temporary, "wb") as target:
            shutil.copyfileobj(source, target)
        os.replace(temporary, dest)


def _original_state(report: IntakeReport, dest_dir: str, inventory: IntakeReport) -> dict:
    # Store state outside the uploaded tree so an archive cannot supply it.
    state_path = report.staging_dir + ".original.json"
    if os.path.exists(state_path):
        with open(state_path) as stream:
            state = json.load(stream)
        root = os.path.realpath(dest_dir)
        if not os.path.realpath(state["target"]).startswith(root + os.sep):
            raise ValueError("Previous import destination escapes the library")
        return state
    album = describe_album(inventory)
    files = inventory.audio + inventory.art + inventory.sidecars
    state = {"album": album, "target": _new_album_dir(dest_dir, album),
             "common": os.path.commonpath([os.path.dirname(f) for f in files])}
    with open(state_path + ".tmp", "w") as stream:
        json.dump(state, stream)
    os.replace(state_path + ".tmp", state_path)
    return state


async def import_original(report: IntakeReport, dest_dir: str,
                          progress: dict | None = None) -> tuple[dict, dict]:
    """Keep audio bytes, filenames, embedded art, lyrics and supported sidecars.

    Existing editions get a new folder. Cancellation leaves the remaining
    files in staging and finishes the current file before returning.
    """
    started = time.monotonic()
    progress = progress if progress is not None else {}
    progress.setdefault("saved", 0)
    inventory = IntakeReport(name=report.name, staging_dir=report.staging_dir)
    if not await asyncio.to_thread(_inventory_tree, report.staging_dir, inventory):
        raise ValueError(inventory.error)
    if not inventory.audio:
        raise ValueError("No audio left in staging")
    state = await asyncio.to_thread(_original_state, report, dest_dir, inventory)
    album, target, common = state["album"], state["target"], state["common"]
    # Copy art first. State keeps the same destination and archive wrapper on retry.
    files = inventory.art + inventory.sidecars + inventory.audio
    for rel in files:
        dest = os.path.join(target, os.path.relpath(rel, common or "."))
        if not os.path.realpath(dest).startswith(os.path.realpath(target) + os.sep):
            raise ValueError("Import destination escapes the album")
        await run_to_completion(asyncio.to_thread(
            _file_original, os.path.join(report.staging_dir, rel), dest,
            progress, rel in inventory.audio))
    # Sidecars are now in the library. Unsupported extras remain recoverable.
    for rel in inventory.art + inventory.sidecars:
        os.remove(os.path.join(report.staging_dir, rel))
    os.remove(report.staging_dir + ".original.json")
    for root, dirs, _files in os.walk(report.staging_dir, topdown=False):
        for directory in dirs:
            path = os.path.join(root, directory)
            if not os.listdir(path):
                os.rmdir(path)
    remaining = [f for f in os.listdir(report.staging_dir) if f != ".upload-name"]
    if not remaining:
        await asyncio.to_thread(shutil.rmtree, report.staging_dir)
    result = _result_dict(target, progress["saved"], 0, [], len(inventory.audio),
                          "Original files", 0, time.monotonic() - started,
                          {"local upload": progress["saved"]})
    return album, result
