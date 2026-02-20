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
        f"https://tidal.com/album/{album_id}"
        f" · Downloaded with github.com/eeegoloauq/music-bot"
    )


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
