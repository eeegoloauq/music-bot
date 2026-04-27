import os
import re
import unicodedata

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp4 import MP4, MP4FreeForm

_AUDIO_EXTS = frozenset({".flac", ".mp3", ".m4a", ".ogg", ".opus", ".aac", ".wv", ".ape"})
_AUDIO_DEDUP_EXTS = (".flac", ".m4a", ".mp3")  # formats we read tags from for dedup
_DEEZER_ID_RE = re.compile(r"deezer\.com/album/(\d+)")


def _sanitize(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip('. ')


def _normalize_title(title: str) -> str:
    """Normalize title for fuzzy matching across different tagging tools.

    Handles: square brackets → parens, feat. suffixes, extra whitespace.
    """
    t = title.replace("[", "(").replace("]", ")")
    t = re.sub(r"\s*\(feat\..*?\)", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+feat\..*$", "", t, flags=re.IGNORECASE)
    return t.strip().lower()


def _track_prefix(disc: int, num: int, total_discs: int) -> str:
    """Return filename prefix: '01' for single-disc, '1-01' for multi-disc."""
    if total_discs > 1:
        return f"{disc}-{num:02d}"
    return f"{num:02d}"


def _comment_value(album_id: str) -> str:
    return (
        f"https://www.deezer.com/album/{album_id}"
        f" · Downloaded with github.com/eeegoloauq/music-bot"
    )


def _cover_url(cover_uuid: str, size: int = 1280) -> str:
    """Resize a stored cover URL to ``{size}x{size}``. The album dict's
    ``cover_uuid`` field actually holds a full Deezer CDN URL (the name is
    historical — kept so renaming the field doesn't ripple through callers).
    """
    if not cover_uuid:
        return ""
    return re.sub(
        r"/\d+x\d+(?:[-\d]+)?\.jpg",
        f"/{size}x{size}-000000-80-0-0.jpg",
        cover_uuid,
    )


def _resolve_dir_canonical(parent: str, name: str) -> str:
    """Return ``<parent>/<name>``, but if a sibling dir exists with the same
    name in different case, rename it to the canonical form first (or merge
    its files into an already-existing canonical dir). Prevents duplicate
    albums like "Joeyy/MIRY" + "Joeyy/Miry" appearing as separate entries
    when the metadata source's casing changes between downloads.
    """
    canonical = os.path.join(parent, name)
    if not os.path.isdir(parent):
        return canonical
    target = name.lower()
    canonical_exists = os.path.isdir(canonical)
    for entry in os.listdir(parent):
        if entry == name:
            continue
        existing = os.path.join(parent, entry)
        if entry.lower() != target or not os.path.isdir(existing):
            continue
        if canonical_exists:
            # Both casings exist; merge non-canonical into canonical, then rmdir
            try:
                for sub in os.listdir(existing):
                    src = os.path.join(existing, sub)
                    dst = os.path.join(canonical, sub)
                    # Preserve canonical's copy if same-named file already there
                    if not os.path.exists(dst):
                        os.rename(src, dst)
                if not os.listdir(existing):
                    os.rmdir(existing)
            except OSError:
                pass
        else:
            try:
                os.rename(existing, canonical)
            except OSError:
                return existing
    return canonical


def _ensure_album_dir(dest_dir: str, artist: str, album_title: str) -> str:
    """Create Artist/Album directory with Samba-compatible permissions.

    Reuses an existing case-insensitive match for either segment (e.g. a pre-
    existing ``Joeyy/`` is reused even if metadata says ``joeyy``), avoiding
    duplicate folders when album titles change casing across metadata sources.
    """
    artist_dir = _resolve_dir_canonical(dest_dir, artist)
    if not os.path.isdir(artist_dir):
        os.makedirs(artist_dir, exist_ok=True)
    album_dir = _resolve_dir_canonical(artist_dir, album_title)
    if not os.path.isdir(album_dir):
        os.makedirs(album_dir, exist_ok=True)
    os.chmod(album_dir, 0o777)
    os.chmod(artist_dir, 0o777)
    return album_dir


def _normalize_for_compare(s: str) -> str:
    """NFC-normalize + casefold + strip — for case/Unicode-insensitive
    comparison of album titles read from peer files vs Deezer metadata."""
    return unicodedata.normalize("NFC", s or "").casefold().strip()


def _read_tags_for_dedup(filepath: str) -> tuple[str, str]:
    """Open one audio file; return ``(comment, album)`` tag values.
    Returns ``("", "")`` on any error or missing tags. Format-agnostic
    via mutagen's auto-detect (``easy=True`` exposes Vorbis/iTunes/ID3
    fields under unified keys)."""
    try:
        f = MutagenFile(filepath, easy=True)
        if f is None or f.tags is None:
            return ("", "")
        comment = next(iter(f.tags.get("comment") or []), "") or ""
        if not comment:
            comment = next(iter(f.tags.get("description") or []), "") or ""
        album = next(iter(f.tags.get("album") or []), "") or ""
        return (str(comment), str(album))
    except Exception:
        return ("", "")


def _folder_signals(folder: str) -> tuple[str | None, str | None]:
    """Read ``(deezer_id, album_tag)`` from any tagged audio file in ``folder``.

    We iterate every file rather than stopping at the first because a
    partially-tagged folder (mid-download race) might have the canonical
    bot ``comment`` on track 1 and a stale peer tag on track 3 — we want
    to find the deezer ID even if ``listdir`` hands us track 3 first."""
    if not os.path.isdir(folder):
        return None, None
    deezer_id: str | None = None
    album_tag: str | None = None
    for fname in sorted(os.listdir(folder)):
        if not fname.lower().endswith(_AUDIO_DEDUP_EXTS):
            continue
        comment, album = _read_tags_for_dedup(os.path.join(folder, fname))
        if deezer_id is None and comment:
            m = _DEEZER_ID_RE.search(comment)
            if m:
                deezer_id = m.group(1)
        if album_tag is None and album:
            album_tag = album
        if deezer_id is not None and album_tag is not None:
            break
    return deezer_id, album_tag


def _locate_existing_album(music_dir: str, album_meta: dict) -> str | None:
    """Find the existing folder for this album under ``music_dir``, if any.

    Identity comes from tags, not folder names — the bot's canonical
    ``comment`` (carrying the Deezer album ID) is authoritative; the
    ``album`` tag is the fallback for legacy folders this bot never
    touched (ripped by beets / Picard / iTunes / manual). Two passes:

      1. Folder whose ``comment`` Deezer ID matches — wins outright,
         even if there's also a legacy folder with a same-title tag.
      2. Folder whose ``album`` tag matches our expected title (NFC +
         casefold). Catches foreign-tooled folders.

    Scope is ``<music_dir>/<artist>/*`` only — going wider would conflate
    same-titled compilations across artists ("Greatest Hits" everywhere)."""
    artist = album_meta.get("artist") or ""
    title = album_meta.get("title") or ""
    deezer_id = str(album_meta.get("id") or "")
    if not artist or not title:
        return None
    artist_dir = _resolve_dir_canonical(music_dir, _sanitize(artist))
    if not os.path.isdir(artist_dir):
        return None
    norm_expected = _normalize_for_compare(title)

    children = sorted(
        os.path.join(artist_dir, c) for c in os.listdir(artist_dir)
        if os.path.isdir(os.path.join(artist_dir, c))
    )
    signals = [(p, *_folder_signals(p)) for p in children]

    # Pass 1: bot-canonical match (authoritative).
    if deezer_id:
        for path, did, _alb in signals:
            if did == deezer_id:
                return path

    # Pass 2: legacy folder match by album tag.
    for path, _did, alb in signals:
        if alb and _normalize_for_compare(alb) == norm_expected:
            return path
    return None


def _find_existing_track(album_dir: str, track: dict) -> str | None:
    """Find an existing audio file for this track in album_dir.

    Checks canonical filenames first (fast), then scans by ISRC and
    tracknumber+title to match files named by other tools/conventions.
    """
    disc = track.get("discNumber", 1)
    num = track.get("trackNumber", 0)
    title = _sanitize(track["title"])

    # Fast path: check both disc-prefixed (multi-disc) and plain (single-disc)
    for ext in (".flac", ".m4a", ".mp3"):
        for prefix in (f"{disc}-{num:02d}", f"{num:02d}"):
            canonical = os.path.join(album_dir, f"{prefix} {title}{ext}")
            if os.path.exists(canonical):
                return canonical

    if not os.path.isdir(album_dir):
        return None

    isrc = (track.get("isrc") or "").upper()
    track_title_norm = _normalize_title(track["title"])

    # Two-pass walk: ISRC and tracknum+title match in pass 1 (strong signals).
    # Title-only match collected in pass 1, returned in pass 2 — same song
    # under a different track number or sanitised title (legacy folders
    # numbered differently than current Deezer release).
    title_only_hit: str | None = None
    for fname in os.listdir(album_dir):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in _AUDIO_EXTS:
            continue
        fpath = os.path.join(album_dir, fname)
        try:
            if ext == ".flac":
                af = FLAC(fpath)
                file_isrc = next(iter(af.get("isrc") or []), "").upper()
                tnum = next(iter(af.get("tracknumber") or ["0"]), "0").split("/")[0].strip()
                ftitle = _normalize_title(next(iter(af.get("title") or [""]), ""))
            elif ext == ".m4a":
                af = MP4(fpath)
                isrc_key = "----:com.apple.iTunes:ISRC"
                raw = af.get(isrc_key, [b""])
                file_isrc = (raw[0].decode("utf-8", errors="ignore")
                             if isinstance(raw[0], (bytes, MP4FreeForm)) else str(raw[0])).upper()
                trkn = af.get("trkn", [(0, 0)])
                tnum = str(trkn[0][0]) if trkn else "0"
                nam = af.get("\xa9nam", [""])
                ftitle = _normalize_title(nam[0] if nam else "")
            else:
                af = MutagenFile(fpath, easy=True)
                if af is None:
                    continue
                file_isrc = next(iter(af.get("isrc") or []), "").upper()
                tnum = next(iter(af.get("tracknumber") or ["0"]), "0").split("/")[0].strip()
                ftitle = _normalize_title(next(iter(af.get("title") or [""]), ""))

            if isrc and file_isrc == isrc:
                return fpath
            if tnum == str(num) and ftitle == track_title_norm:
                return fpath
            if ftitle and ftitle == track_title_norm and title_only_hit is None:
                title_only_hit = fpath
        except Exception:
            continue
    return title_only_hit
