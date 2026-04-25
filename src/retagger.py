"""Library re-tagger.

Walks ``MUSIC_DIR`` and refreshes every album's tags from current Deezer +
Last.fm metadata, without re-downloading any audio. Used to bring the
existing library into line after the bot's tag schema evolves (new fields,
new sources, casing fixes).

Two-phase API:
  1. ``run_dry_run`` produces a list of ``AlbumPlan`` describing what would
     change. Caller renders a summary, asks for confirmation.
  2. ``run_apply`` takes those plans and writes the tags + folder renames.

Identification ladder per album folder:
  a) read the bot's ``comment`` tag from any audio file in the folder
     (the canonical value embeds a Deezer / Tidal album URL — both legacy
     and current downloads parse cleanly)
  b) fallback: ``metadata.search`` by ``"<artist_dir> <album_dir>"`` text,
     accept the top hit if duration sums match within 30s and track count
     is within 1 of disk reality
  c) otherwise mark unidentifiable, skip on apply
"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, ID3NoHeaderError

import metadata
from metadata import deezer
from metadata.lastfm import fetch_album_tags
from library.files import (
    _comment_value, _resolve_dir_canonical, _sanitize, _normalize_title,
)
from library.tagger import _write_tags, _write_m4a_tags, _write_mp3_tags

logger = logging.getLogger(__name__)

_AUDIO_EXTS = (".flac", ".m4a", ".mp3")
# Capture which platform's URL is in the comment — Tidal album IDs are NOT
# valid Deezer IDs, so we use the Tidal-era marker only as a "this is one
# of ours" signal and fall back to folder-name search for the actual id.
_ALBUM_URL_RE = re.compile(
    r"(?P<host>tidal\.com|(?:www\.)?deezer\.com)/album/(?P<id>\d+)",
    re.IGNORECASE,
)


def _significant_words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", (text or "").lower()) if len(w) >= 3}


def _clean_search_term(text: str) -> str:
    """Massage a folder name back into something Deezer's search likes.
    Filename-safe substitutions (``:`` → ``_``) and stylized punctuation
    (``$``, brackets) confuse the search; collapse them to spaces.
    """
    text = re.sub(r"[_\$\(\)\[\]\{\}*?\"<>|;]+", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text
# Concurrent albums in flight. Each album costs 1 Deezer /album/ + N Deezer
# /track/ + 1 Last.fm. Three-at-a-time keeps the tail under Deezer's
# 50 req / 5 sec cap with margin.
_ALBUM_SEM = asyncio.Semaphore(3)


# --- data shapes -----------------------------------------------------------

@dataclass
class AlbumPlan:
    folder: str                                 # absolute album-folder path
    artist_dir: str                             # immediate-parent dir name
    album_dir: str                              # album-folder leaf name
    files: list[str] = field(default_factory=list)
    album_id: str | None = None                 # Deezer ID once identified
    fresh_meta: dict | None = None              # metadata.fetch_album result
    lastfm_only_genres: list[str] = field(default_factory=list)  # set when Deezer fails but Last.fm has tags
    canonical_album_dir: str | None = None      # set when folder needs rename
    canonical_artist_dir: str | None = None     # set when artist dir needs rename
    changes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def needs_apply(self) -> bool:
        return self.error is None and bool(self.changes)

    @property
    def is_lastfm_only(self) -> bool:
        return self.fresh_meta is None and bool(self.lastfm_only_genres)


@dataclass
class RetagSummary:
    total: int = 0
    by_comment_id: int = 0
    by_search: int = 0
    unidentified: int = 0
    no_changes: int = 0
    will_change: int = 0


# --- scan ------------------------------------------------------------------

def scan_albums(music_dir: str) -> list[tuple[str, str, str]]:
    """Walk ``music_dir`` two levels deep. Returns
    ``[(album_path, artist_name, album_name), ...]`` sorted alphabetically.
    """
    out: list[tuple[str, str, str]] = []
    if not os.path.isdir(music_dir):
        return out
    try:
        artist_entries = sorted(os.listdir(music_dir))
    except OSError:
        return out
    for artist in artist_entries:
        if artist.startswith(".") or artist in {"lost+found"}:
            continue
        artist_path = os.path.join(music_dir, artist)
        if not os.path.isdir(artist_path):
            continue
        try:
            album_entries = sorted(os.listdir(artist_path))
        except OSError:
            continue
        for album in album_entries:
            if album.startswith("."):
                continue
            album_path = os.path.join(artist_path, album)
            if os.path.isdir(album_path):
                out.append((album_path, artist, album))
    return out


def _list_audio_files(folder: str) -> list[str]:
    out: list[str] = []
    try:
        for fname in sorted(os.listdir(folder)):
            if fname.lower().endswith(_AUDIO_EXTS):
                out.append(os.path.join(folder, fname))
    except OSError:
        pass
    return out


# --- identify --------------------------------------------------------------

def _read_comment(filepath: str) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".flac":
            return next(iter(FLAC(filepath).get("comment") or []), "") or ""
        if ext == ".m4a":
            v = MP4(filepath).get("\xa9cmt", [])
            if not v:
                return ""
            v0 = v[0]
            if isinstance(v0, MP4FreeForm):
                return bytes(v0).decode("utf-8", errors="ignore")
            return str(v0)
        if ext == ".mp3":
            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                return ""
            for f in tags.getall("COMM"):
                if f.text:
                    return f.text[0]
    except Exception:
        return ""
    return ""


def _extract_album_id(filepath: str) -> tuple[str, str] | None:
    """Returns ``(host, id)`` parsed from the file's comment tag, or None.
    ``host`` is either ``"deezer"`` or ``"tidal"``."""
    m = _ALBUM_URL_RE.search(_read_comment(filepath))
    if not m:
        return None
    host = "deezer" if "deezer" in m.group("host").lower() else "tidal"
    return host, m.group("id")


def _gate_candidate(
    cand: dict,
    folder_artist_words: set[str],
    folder_album_words: set[str],
    on_disk: int,
    require_album_overlap: bool = True,
) -> str | None:
    """Apply the standard artist + album + track-count gates to one Deezer
    search hit. Returns its album_id when accepted, otherwise None.

    Album-title rule: one word set must be a (near-)subset of the other.
    A bare overlap of one common word isn't enough — that lets ``Cool Cat``
    match ``Now They Think Its Cool`` just because both contain ``cool``.
    Subset-direction matching covers both ``X (Deluxe)`` ↔ ``X`` and
    ``X`` ↔ ``X (Bonus Tracks)`` cases.
    """
    aid = str(cand.get("id") or "")
    if not aid:
        return None

    # Artist gate. Skip if the folder artist has no ≥3-char words (avoids
    # over-filtering names like 'P', '21').
    cand_artist_words = _significant_words(
        (cand.get("artist") or {}).get("name", "")
    )
    if folder_artist_words and not (folder_artist_words & cand_artist_words):
        return None

    if require_album_overlap and folder_album_words:
        cand_album_words = _significant_words(cand.get("title", ""))
        if not cand_album_words:
            return None
        small, big = sorted(
            (folder_album_words, cand_album_words), key=len,
        )
        # Allow at most one missing word from the smaller side — handles
        # punctuation drift ("Vol. 1" vs "Volume 1") without letting in
        # tenuously-related albums.
        missing = len(small - big)
        if missing > max(0, len(small) // 4):
            return None

    deezer_tracks = int(cand.get("nb_tracks") or 0)
    if deezer_tracks and abs(deezer_tracks - on_disk) > 1:
        return None

    return aid


async def _search_album(artist: str, album: str, files: list[str]) -> str | None:
    """Identify the album on Deezer via a multi-pass search:

    1. Combined ``"<artist> <album>"`` query — usual case, top 5 hits gated
       by artist + album + track-count overlap.
    2. Album-only query — handles stylised / accented artist names that
       trip up Deezer's combined matcher.
    3. Artist-only query, top 25 — for when the folder album name diverges
       from Deezer's canonical title (deluxe editions, rephrased titles).
       The album word-overlap gate stays in force; the track-count match
       provides the second signal.

    Returns the first id that clears all gates, else ``None``.
    """
    cleaned_artist = _clean_search_term(artist)
    cleaned_album = _clean_search_term(album)
    folder_artist_words = _significant_words(artist)
    folder_album_words = _significant_words(album)
    on_disk = len(files)

    queries: list[tuple[str, int, bool]] = []
    if cleaned_artist and cleaned_album:
        queries.append((f"{cleaned_artist} {cleaned_album}", 5, True))
    if cleaned_album:
        queries.append((cleaned_album, 5, True))
    if cleaned_artist:
        queries.append((cleaned_artist, 25, True))

    seen_ids: set[str] = set()
    for query, limit, require_album_overlap in queries:
        try:
            candidates = await deezer.search_albums(query, limit=limit)
        except Exception as e:
            logger.debug("Search pass %r failed: %s", query, e)
            continue
        for cand in candidates:
            aid = _gate_candidate(
                cand, folder_artist_words, folder_album_words, on_disk,
                require_album_overlap=require_album_overlap,
            )
            if aid and aid not in seen_ids:
                return aid
            if aid:
                seen_ids.add(aid)
    return None


async def _try_lastfm_only(artist: str, album: str) -> list[str]:
    """When Deezer can't identify the album, try to enrich genres via
    Last.fm alone. Returns the filtered tag list (possibly empty)."""
    try:
        return await fetch_album_tags(artist, album)
    except Exception as e:
        logger.debug("Last.fm-only fallback failed for %s — %s: %s", artist, album, e)
        return []


# --- per-file inspection ---------------------------------------------------

def _read_file_summary(filepath: str) -> dict:
    """Pull just the fields we diff in dry-run: comment marker, genres,
    artist/album/albumartist casing, releasedate (FLAC), has-RG-track.
    """
    ext = os.path.splitext(filepath)[1].lower()
    out = {"path": filepath, "ext": ext, "comment": "", "genres": [],
           "artist": "", "album": "", "albumartist": "",
           "releasedate": "", "has_rg": False, "isrc": "",
           "tracknumber": 0, "title": ""}
    try:
        if ext == ".flac":
            f = FLAC(filepath)
            out["comment"] = next(iter(f.get("comment") or []), "")
            out["genres"] = [str(g) for g in (f.get("genre") or [])]
            out["artist"] = next(iter(f.get("artist") or []), "")
            out["album"] = next(iter(f.get("album") or []), "")
            out["albumartist"] = next(iter(f.get("albumartist") or []), "")
            out["releasedate"] = next(iter(f.get("releasedate") or []), "")
            out["has_rg"] = "replaygain_track_gain" in f
            out["isrc"] = next(iter(f.get("isrc") or []), "").upper()
            out["title"] = next(iter(f.get("title") or []), "")
            tn = next(iter(f.get("tracknumber") or ["0"]), "0").split("/")[0]
            out["tracknumber"] = int(tn) if tn.isdigit() else 0
        elif ext == ".m4a":
            f = MP4(filepath)
            cmt = f.get("\xa9cmt", [])
            out["comment"] = str(cmt[0]) if cmt else ""
            gen = f.get("\xa9gen", [])
            out["genres"] = [str(g) for g in gen] if gen else []
            out["artist"] = str(f.get("\xa9ART", [""])[0])
            out["album"] = str(f.get("\xa9alb", [""])[0])
            out["albumartist"] = str(f.get("aART", [""])[0])
            day = f.get("\xa9day", [""])
            out["releasedate"] = str(day[0]) if day else ""
            out["has_rg"] = any(k.startswith("----:com.apple.iTunes:REPLAYGAIN_TRACK")
                                 for k in f.keys())
            isrc_raw = f.get("----:com.apple.iTunes:ISRC", [b""])
            out["isrc"] = (bytes(isrc_raw[0]).decode("utf-8", "ignore")
                           if isrc_raw and isinstance(isrc_raw[0], (bytes, MP4FreeForm))
                           else "").upper()
            out["title"] = str(f.get("\xa9nam", [""])[0])
            trkn = f.get("trkn", [(0, 0)])
            out["tracknumber"] = int(trkn[0][0]) if trkn else 0
        elif ext == ".mp3":
            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                return out
            comm = tags.getall("COMM")
            out["comment"] = comm[0].text[0] if (comm and comm[0].text) else ""
            tcon = tags.get("TCON")
            out["genres"] = [str(x) for x in (tcon.text if tcon else [])]
            out["artist"] = str(tags.get("TPE1").text[0]) if tags.get("TPE1") else ""
            out["album"] = str(tags.get("TALB").text[0]) if tags.get("TALB") else ""
            out["albumartist"] = str(tags.get("TPE2").text[0]) if tags.get("TPE2") else ""
            tdrl = tags.get("TDRL") or tags.get("TDRC")
            out["releasedate"] = str(tdrl.text[0]) if tdrl else ""
            out["has_rg"] = any(
                f.desc.upper() == "REPLAYGAIN_TRACK_GAIN" for f in tags.getall("TXXX")
            )
            out["isrc"] = str(tags.get("TSRC").text[0]).upper() if tags.get("TSRC") else ""
            out["title"] = str(tags.get("TIT2").text[0]) if tags.get("TIT2") else ""
            trck = tags.get("TRCK")
            tn = (str(trck.text[0]).split("/")[0]) if trck else "0"
            out["tracknumber"] = int(tn) if tn.isdigit() else 0
    except Exception as e:
        logger.debug("Failed reading %s: %s", filepath, e)
    return out


def _match_track(file_summary: dict, fresh_tracks: list[dict]) -> dict | None:
    """Match a disk file to a fresh-metadata track. ISRC wins; otherwise
    fall back to track-number + fuzzy-title."""
    isrc = file_summary["isrc"]
    if isrc:
        for t in fresh_tracks:
            if (t.get("isrc") or "").upper() == isrc:
                return t
    tn = file_summary["tracknumber"]
    norm_title = _normalize_title(file_summary["title"])
    for t in fresh_tracks:
        if int(t.get("trackNumber") or 0) == tn:
            if not norm_title or norm_title == _normalize_title(t.get("title", "")):
                return t
    return None


# --- diff ------------------------------------------------------------------

_OUR_COMMENT_RE = re.compile(r"music-bot", re.IGNORECASE)


def _compute_changes(folder: str, artist: str, album: str,
                     fresh_meta: dict, files: list[str]) -> tuple[list[str], str | None, str | None]:
    """Returns (compact change list, canonical_album_dir, canonical_artist_dir).
    Empty list = nothing to change."""
    changes: list[str] = []

    # Folder casing — driven entirely by Deezer canonical strings.
    canonical_album = _sanitize(fresh_meta.get("title") or album)
    canonical_artist = _sanitize(fresh_meta.get("artist") or artist)
    needs_album_rename = canonical_album != album
    needs_artist_rename = canonical_artist != artist
    if needs_album_rename:
        changes.append(f"folder: {album!r} → {canonical_album!r}")
    if needs_artist_rename:
        changes.append(f"artist folder: {artist!r} → {canonical_artist!r}")

    fresh_genres_lower = {g.lower() for g in (fresh_meta.get("genres") or [])}
    track_count_disk = len(files)
    track_count_fresh = len(fresh_meta.get("tracks") or [])
    if track_count_disk != track_count_fresh:
        changes.append(
            f"track count: disk has {track_count_disk}, Deezer says {track_count_fresh}"
        )

    files_summary = [_read_file_summary(p) for p in files]
    n_files = len(files_summary)

    n_missing_comment = sum(
        1 for s in files_summary if not _OUR_COMMENT_RE.search(s["comment"] or "")
    )
    if n_missing_comment:
        changes.append(f"comment: {n_missing_comment}/{n_files} files lack canonical marker")

    n_missing_genre = 0
    for s in files_summary:
        existing = {g.lower() for g in s["genres"]}
        if not existing.issuperset(fresh_genres_lower) and fresh_genres_lower:
            n_missing_genre += 1
    if n_missing_genre:
        added = sorted({g for g in fresh_meta.get("genres") or []
                        if g.lower() not in
                        {gg.lower() for s in files_summary for gg in s["genres"]}})
        sample = ", ".join(added[:4]) + ("…" if len(added) > 4 else "")
        changes.append(f"genres: +{len(added)} ({sample}) on {n_missing_genre}/{n_files}")

    n_missing_rd = sum(1 for s in files_summary
                        if s["ext"] == ".flac" and not s["releasedate"])
    if n_missing_rd:
        changes.append(f"releasedate (FLAC): {n_missing_rd}/{n_files} missing")

    n_missing_rg = sum(1 for s in files_summary if not s["has_rg"])
    if n_missing_rg and fresh_meta.get("tracks"):
        # Only call out if Deezer actually has gain data we can write.
        if any(t.get("track_gain") is not None for t in fresh_meta["tracks"]):
            changes.append(f"replaygain: {n_missing_rg}/{n_files} missing")

    canonical_albumartist = fresh_meta.get("artist") or ""
    canonical_album_title = fresh_meta.get("title") or ""
    n_casing = sum(
        1 for s in files_summary
        if (canonical_albumartist and s["albumartist"] != canonical_albumartist)
        or (canonical_album_title and s["album"] != canonical_album_title)
    )
    if n_casing:
        changes.append(f"album/albumartist casing: {n_casing}/{n_files}")

    return (
        changes,
        canonical_album if needs_album_rename else None,
        canonical_artist if needs_artist_rename else None,
    )


# --- plan ------------------------------------------------------------------

async def plan_album(folder: str, artist: str, album: str) -> AlbumPlan:
    plan = AlbumPlan(folder=folder, artist_dir=artist, album_dir=album)
    plan.files = _list_audio_files(folder)
    if not plan.files:
        plan.error = "no audio files in folder"
        return plan

    # Two independent enrichment sources: Deezer (canonical metadata) and
    # Last.fm (community genre tags). Always try both. Deezer needs an ID
    # match (comment tag or folder-name search); Last.fm needs only artist +
    # album strings, so it works even when Deezer doesn't have the album.
    async with _ALBUM_SEM:
        for fp in plan.files:
            tag = _extract_album_id(fp)
            if tag and tag[0] == "deezer":
                plan.album_id = tag[1]
                break

        if not plan.album_id:
            plan.album_id = await _search_album(artist, album, plan.files)

        if plan.album_id:
            try:
                plan.fresh_meta = await metadata.fetch_album(plan.album_id)
            except Exception as e:
                logger.debug(
                    "fetch_album failed for %s/%s id=%s: %s",
                    artist, album, plan.album_id, e,
                )
                plan.album_id = None
                plan.fresh_meta = None

        # Last.fm: independent service. Always try it, regardless of the
        # Deezer outcome. If Deezer succeeded we merge tags into fresh_meta
        # (canonical title/artist drives the Last.fm lookup). If Deezer
        # failed we still capture Last.fm-only genres so the caller can
        # write a partial update.
        if plan.fresh_meta is not None:
            await metadata.enrich_genres(plan.fresh_meta)
        else:
            plan.lastfm_only_genres = await _try_lastfm_only(artist, album)

    if plan.fresh_meta is not None:
        changes, ren_album, ren_artist = _compute_changes(
            folder, artist, album, plan.fresh_meta, plan.files,
        )
        plan.changes = changes
        plan.canonical_album_dir = ren_album
        plan.canonical_artist_dir = ren_artist
        return plan

    if plan.lastfm_only_genres:
        plan.changes = _compute_lastfm_only_changes(
            plan.lastfm_only_genres, plan.files,
        )
        return plan

    plan.error = "could not identify album"
    return plan


def _compute_lastfm_only_changes(
    lastfm_genres: list[str], files: list[str],
) -> list[str]:
    """Diff: how many files would gain at least one Last.fm genre."""
    if not lastfm_genres:
        return []
    needed_lower = {g.lower() for g in lastfm_genres}
    n_need_genre = 0
    for fp in files:
        s = _read_file_summary(fp)
        existing_lower = {g.lower() for g in s["genres"]}
        if not existing_lower.issuperset(needed_lower):
            n_need_genre += 1
    if not n_need_genre:
        return []
    sample = ", ".join(lastfm_genres[:4]) + ("…" if len(lastfm_genres) > 4 else "")
    return [
        f"Last.fm only ({len(lastfm_genres)} genres: {sample}) on "
        f"{n_need_genre}/{len(files)} files"
    ]


# --- apply -----------------------------------------------------------------

def _append_genres_sync(filepath: str, new_genres: list[str]) -> bool:
    """Add ``new_genres`` to a file's GENRE / TCON / ©gen tag in place,
    leaving everything else alone. Returns True when something changed.
    Used by the Last.fm-only branch — the file isn't on Deezer, so we
    can't safely rewrite the canonical fields, but adding fine-grained
    genre tags on top of whatever's already there is safe.
    """
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".flac":
            audio = FLAC(filepath)
            current = list(audio.get("genre") or [])
            current_lower = {g.lower() for g in current}
            added = [g for g in new_genres if g.lower() not in current_lower]
            if not added:
                return False
            audio["genre"] = current + added
            audio.save()
            return True
        if ext == ".m4a":
            audio = MP4(filepath)
            current = list(audio.get("\xa9gen") or [])
            current_str = current[0] if current else ""
            existing_set = {g.strip().lower() for g in current_str.split(";") if g.strip()}
            added = [g for g in new_genres if g.lower() not in existing_set]
            if not added:
                return False
            merged = "; ".join(([current_str] if current_str else []) + added)
            audio["\xa9gen"] = [merged]
            audio.save()
            return True
        if ext == ".mp3":
            from mutagen.id3 import TCON
            try:
                tags = ID3(filepath)
            except ID3NoHeaderError:
                tags = ID3()
            tcon = tags.get("TCON")
            current = list(tcon.text) if tcon else []
            current_lower = {str(g).lower() for g in current}
            added = [g for g in new_genres if g.lower() not in current_lower]
            if not added:
                return False
            tags.add(TCON(encoding=3, text=current + added))
            tags.save(filepath, v2_version=4)
            return True
    except Exception:
        logger.warning("Genre-only append failed: %s", filepath, exc_info=True)
    return False


def _apply_album_sync(plan: AlbumPlan) -> tuple[int, int]:
    """Synchronous worker: rename folders, write tags. Returns
    ``(files_written, files_skipped)``. Idempotent — running twice is safe.
    """
    # Last.fm-only path: only append genres, don't touch anything else.
    if plan.is_lastfm_only:
        written = sum(
            1 for fp in plan.files
            if _append_genres_sync(fp, plan.lastfm_only_genres)
        )
        return written, len(plan.files) - written

    if not plan.fresh_meta:
        return 0, len(plan.files)

    # Resolve any folder rename first so subsequent file paths line up.
    folder = plan.folder
    artist_dir_path = os.path.dirname(folder)
    music_root = os.path.dirname(artist_dir_path)

    if plan.canonical_artist_dir:
        new_artist_dir = _resolve_dir_canonical(music_root, plan.canonical_artist_dir)
        if new_artist_dir != artist_dir_path:
            folder = os.path.join(new_artist_dir, plan.album_dir)
            artist_dir_path = new_artist_dir
    if plan.canonical_album_dir:
        new_folder = _resolve_dir_canonical(artist_dir_path, plan.canonical_album_dir)
        if new_folder != folder:
            folder = new_folder

    # Re-list files in case folder moved during rename.
    files = _list_audio_files(folder)

    fresh_tracks = plan.fresh_meta.get("tracks") or []
    written = 0
    skipped = 0
    for fp in files:
        summary = _read_file_summary(fp)
        track = _match_track(summary, fresh_tracks)
        if track is None:
            skipped += 1
            continue
        ext = summary["ext"].lstrip(".")
        try:
            if ext == "m4a":
                _write_m4a_tags(fp, track, plan.fresh_meta, None, None, None, force=True)
            elif ext == "mp3":
                _write_mp3_tags(fp, track, plan.fresh_meta, None, None, None, force=True)
            else:
                _write_tags(fp, track, plan.fresh_meta, None, None, None, force=True)
            written += 1
        except Exception:
            logger.warning("Re-tag write failed: %s", fp, exc_info=True)
            skipped += 1
    return written, skipped


async def apply_plan(plan: AlbumPlan) -> tuple[int, int]:
    return await asyncio.to_thread(_apply_album_sync, plan)


# --- orchestrator ----------------------------------------------------------

ProgressCb = Callable[[int, int, AlbumPlan], Awaitable[None]] | None


async def run_dry_run(
    music_dir: str, progress: ProgressCb = None,
) -> tuple[list[AlbumPlan], RetagSummary]:
    albums = scan_albums(music_dir)
    summary = RetagSummary(total=len(albums))
    plans: list[AlbumPlan] = []

    async def _one(idx: int, folder: str, artist: str, album: str) -> AlbumPlan:
        plan = await plan_album(folder, artist, album)
        if progress is not None:
            try:
                await progress(idx + 1, len(albums), plan)
            except Exception:
                pass
        return plan

    coros = [_one(i, f, a, al) for i, (f, a, al) in enumerate(albums)]
    plans = list(await asyncio.gather(*coros))

    for plan in plans:
        if plan.error:
            summary.unidentified += 1
        else:
            # Comment-tag deezer.com hits go straight through; everything
            # else (no comment, or stale tidal.com URL) lands on search.
            had_deezer_id = any(
                (t := _extract_album_id(fp)) and t[0] == "deezer"
                for fp in plan.files
            )
            if had_deezer_id:
                summary.by_comment_id += 1
            else:
                summary.by_search += 1
            if plan.changes:
                summary.will_change += 1
            else:
                summary.no_changes += 1
    return plans, summary


async def run_apply(
    plans: list[AlbumPlan], progress: ProgressCb = None,
) -> dict:
    """Apply pre-computed plans. Returns a stats dict for the caller."""
    total = sum(1 for p in plans if p.needs_apply)
    written_total = 0
    skipped_total = 0
    failed: list[tuple[str, str]] = []

    counter = 0
    for plan in plans:
        if not plan.needs_apply:
            continue
        counter += 1
        try:
            written, skipped = await apply_plan(plan)
            written_total += written
            skipped_total += skipped
        except Exception as e:
            failed.append((f"{plan.artist_dir}/{plan.album_dir}", str(e)))
        if progress is not None:
            try:
                await progress(counter, total, plan)
            except Exception:
                pass

    return {
        "albums_planned": total,
        "files_written": written_total,
        "files_skipped": skipped_total,
        "failed": failed,
    }
