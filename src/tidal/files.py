import os
import re

from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.mp4 import MP4, MP4FreeForm

_AUDIO_EXTS = frozenset({".flac", ".mp3", ".m4a", ".ogg", ".opus", ".aac", ".wv", ".ape"})


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


def _tidal_cover_url(cover_uuid: str, size: int = 1280) -> str:
    """Cover URL helper. Accepts either a full image URL (Deezer pattern —
    passthrough with size substitution) or a legacy Tidal UUID with dashes.
    """
    if not cover_uuid:
        return ""
    if cover_uuid.startswith(("http://", "https://")):
        return re.sub(
            r"/\d+x\d+(?:[-\d]+)?\.jpg",
            f"/{size}x{size}-000000-80-0-0.jpg",
            cover_uuid,
        )
    return f"https://resources.tidal.com/images/{cover_uuid.replace('-', '/')}/{size}x{size}.jpg"


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


def _find_existing_track(album_dir: str, track: dict) -> str | None:
    """Find an existing audio file for this track in album_dir.

    Checks canonical filenames first (fast), then scans by ISRC and
    tracknumber+title to match files named by other tools/conventions.
    """
    disc = track.get("discNumber", 1)
    num = track.get("trackNumber", 0)
    title = _sanitize(track["title"])

    # Fast path: check both disc-prefixed (multi-disc) and plain (single-disc)
    for ext in (".flac", ".m4a"):
        for prefix in (f"{disc}-{num:02d}", f"{num:02d}"):
            canonical = os.path.join(album_dir, f"{prefix} {title}{ext}")
            if os.path.exists(canonical):
                return canonical

    if not os.path.isdir(album_dir):
        return None

    isrc = (track.get("isrc") or "").upper()
    track_title_norm = _normalize_title(track["title"])

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
        except Exception:
            continue
    return None
