"""High-level matching: turn a Deezer album/track into the best slskd choice.

Two-stage strategy for albums:

  1. Album-folder search (``"Artist Album"``) — find a single peer who owns the
     whole album. One peer beats stitched-from-many: single connection, single
     queue position, consistent metadata.

  2. Per-track fallback — when no peer has the full album, iterate per track
     and pick the best match each time.

Single-track downloads skip stage 1 entirely.
"""

import asyncio
import logging
import re
import unicodedata

from soulseek import client as slskd
from soulseek.client import SearchResult
from soulseek.scorer import (
    ScoredTrack,
    ScoredFolder,
    score_track_results,
    score_folder_results,
)

logger = logging.getLogger(__name__)


# Score thresholds for auto-pick vs picker vs reject.
TRACK_AUTO_THRESHOLD = 70.0   # ≥ 70 — download without asking
TRACK_PICK_THRESHOLD = 45.0   # 45..70 — show picker; <45 — give up

# Mp3-fallback gates. Triggered only when accept_lossy=True (after a FLAC
# search came up empty). Lower bound on mp3 quality keeps us from saving
# 128kbps junk to disk; m4a is allowed without a bitrate floor because it
# might be ALAC (lossless) — we can't tell without parsing the stream.
_LOSSY_FALLBACK_EXTS = {"mp3", "m4a"}
_LOSSY_MIN_MP3_BITRATE = 256_000


def _is_acceptable_lossy(r) -> bool:
    if r.extension not in _LOSSY_FALLBACK_EXTS:
        return False
    if r.extension == "mp3" and (r.bit_rate or 0) < _LOSSY_MIN_MP3_BITRATE:
        return False
    return True


def _ascii_fold(s: str) -> str:
    """Strip combining marks. Used to build fallback queries for peers who
    name files without diacritics. Slskd's text search is *case-insensitive
    but accent-sensitive* — so "racine carree" won't match "Racine carrée"
    in peer file indexes. We send the original first and the folded form as
    a fallback."""
    nf = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in nf if not unicodedata.combining(c))


_PARENS_VERSION = re.compile(
    r"\s*[\(\[](?:explicit|clean|deluxe|remaster(?:ed)?|expanded|anniversary|"
    r"special|bonus[^)\]]*|.*\bversion\b[^)\]]*)[\)\]]",
    re.IGNORECASE,
)


def _clean_query(text: str) -> str:
    """Strip noise that wouldn't help a Soulseek text search. Keeps diacritics —
    they're load-bearing for slskd's accent-sensitive index."""
    text = _PARENS_VERSION.sub("", text or "")
    # Drop punctuation slskd treats poorly; keep $ for stylized names like A$AP.
    text = re.sub(r"[\\/:*?\"<>|\[\]\(\)]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_JUNK_RE = re.compile(r"[^\w\s'-]", re.UNICODE)


def _has_diacritics(s: str) -> bool:
    nf = unicodedata.normalize("NFKD", s or "")
    return any(unicodedata.combining(c) for c in nf)


def _junk_ratio(s: str) -> float:
    """Detect titles that are mostly emoji/symbol soup. Counts both punctuation
    junk (●▓⊠®©) AND characters outside the Latin/Cyrillic/CJK alphabets we
    expect from real album metadata. A ratio above 0.2 means the title is
    likely not what peers indexed it under — fall back to artist-only."""
    if not s:
        return 0.0
    bad = 0
    for c in s:
        if c.isspace() or c in "'-":
            continue
        if _JUNK_RE.match(c):
            bad += 1
            continue
        # Word char but in some random script (Telugu ఠ, Kannada ಠ, etc.)?
        # `\w` is too permissive; restrict to scripts that actual album
        # metadata uses.
        cat = unicodedata.name(c, "")
        if not any(s in cat for s in ("LATIN", "CYRILLIC", "DIGIT", "CJK", "HIRAGANA", "KATAKANA", "HANGUL", "GREEK", "ARABIC", "HEBREW")):
            bad += 1
    return bad / max(len(s), 1)


def _token_rescue(s: str) -> str:
    """Pull only the longer ASCII alphanumeric tokens out of a noisy title.
    For input like ``ఠ:g()((⊠tt●)..E.leC(t)®▓N⑄iఠ..mಠUS1`` returns
    ``leC mUS1`` — fragments that may overlap with how peers tagged the
    canonical name."""
    return " ".join(re.findall(r"[A-Za-z0-9]{3,}", s or ""))


def _build_album_queries(artist: str, album: str) -> tuple[str | None, list[str]]:
    """Returns (primary, fallbacks). The primary query is the cleaned
    ``"Artist Title"``; the fallbacks list only fires when the primary
    underperforms (see ``find_album`` escalation gate). Each fallback
    targets a specific failure mode rather than blanket-doubling load."""
    a = _clean_query(artist)
    b = _clean_query(album)

    if a and b:
        primary = f"{a} {b}"
    elif b:
        primary = b
    elif a:
        primary = a
    else:
        return None, []

    fallbacks: list[str] = []
    # Title-only — catches OSTs / compilations where the peer tagged a
    # different artist than Deezer.
    if a and b and a.lower() != b.lower():
        fallbacks.append(b)
    # ASCII-fold — for peers whose filenames lack diacritics.
    if _has_diacritics(primary):
        fallbacks.append(_ascii_fold(primary))
        if a and b and a.lower() != b.lower():
            fallbacks.append(_ascii_fold(b))
    # Garbage-symbol rescue — title is mostly noise, keep just the
    # longest ASCII fragments + artist alone as a last-resort sweep.
    if b and _junk_ratio(b) > 0.2:
        rescued = _token_rescue(b)
        if a and rescued:
            fallbacks.append(f"{a} {rescued}")
        if a:
            fallbacks.append(a)

    return primary, _dedup_lower([primary] + fallbacks)[1:]


def _build_track_queries(artist: str, title: str) -> tuple[str | None, list[str]]:
    a = _clean_query(artist)
    t = _clean_query(title)

    if a and t:
        primary = f"{a} {t}"
    elif t:
        primary = t
    elif a:
        primary = a
    else:
        return None, []

    fallbacks: list[str] = []
    if a and t:
        fallbacks.append(t)
    if _has_diacritics(primary):
        fallbacks.append(_ascii_fold(primary))
        if a and t:
            fallbacks.append(_ascii_fold(t))

    return primary, _dedup_lower([primary] + fallbacks)[1:]


def _dedup_lower(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for q in items:
        ql = q.lower()
        if not q or ql in seen:
            continue
        seen.add(ql)
        out.append(q)
    return out


# --- album matching ---------------------------------------------------------

async def find_album(
    album_meta: dict,
    duration_tolerance: int = 5,
) -> tuple[ScoredFolder | None, list[ScoredFolder]]:
    """Search for a peer-folder that holds the whole album. Returns
    ``(best, alternatives)`` — best is the highest-scored folder regardless
    of coverage; the downloader applies its own coverage threshold to decide
    whether to use it whole, partial, or fall back to per-track."""
    artist = album_meta.get("artist", "")
    title = album_meta.get("title", "")
    tracks = album_meta.get("tracks", [])
    track_durations = [int(t.get("duration", 0) or 0) for t in tracks]
    track_titles = [t.get("title", "") for t in tracks]

    primary, fallbacks = _build_album_queries(artist, title)
    if not primary:
        return None, []

    sem = asyncio.Semaphore(2)
    all_folders: dict[tuple[str, str], slskd.PeerFolder] = {}

    async def _run_query(q: str) -> None:
        async with sem:
            responses = await slskd.search(q, timeout_secs=20, response_limit=200)
        results = slskd.parse_files(responses, lossless_only=True)
        for f in slskd.group_by_folder(results):
            all_folders.setdefault((f.username, f.directory), f)

    def _rescore() -> list[ScoredFolder]:
        if not all_folders:
            return []
        return score_folder_results(
            list(all_folders.values()),
            track_durations=track_durations,
            track_titles=track_titles,
            album_artist=artist,
            album_title=title,
            duration_tolerance=duration_tolerance,
        )

    # Tier 1 — primary query alone. Most popular albums hit a complete folder
    # match here and we never touch slskd again for this album.
    await _run_query(primary)
    scored = _rescore()
    if scored and scored[0].missing_count == 0:
        return scored[0], scored[1:6]

    # Tier 2 — fallbacks only if primary didn't yield a complete-folder match.
    # The fallbacks were chosen by `_build_album_queries` to target specific
    # failure modes (title-only / ASCII-fold / junk-symbol rescue).
    if fallbacks:
        await asyncio.gather(*[_run_query(q) for q in fallbacks])
        scored = _rescore()

    if not scored:
        return None, []

    best = scored[0]
    alternatives = scored[1:6]   # cap returned alternatives to keep UI manageable

    # Always return the best folder candidate. The downloader's coverage
    # threshold decides whether to use it whole, partial+gap-fill, or skip
    # it entirely — keeping that policy in one place.
    return best, alternatives


# --- per-track matching ----------------------------------------------------

async def find_track(
    track_meta: dict,
    album_artist: str | None = None,
    duration_tolerance: int = 5,
    accept_lossy: bool = False,
) -> tuple[ScoredTrack | None, list[ScoredTrack]]:
    """Search and score for a single track. Returns ``(auto_pick, alternatives)``.

    ``auto_pick`` is filled in when score >= TRACK_AUTO_THRESHOLD; otherwise the
    caller can use the ``alternatives`` list (still already filtered & sorted).

    ``accept_lossy`` (mp3-fallback path): instead of FLAC-only, search for
    mp3≥256kbps + m4a, score them with a flat 5pt quality reward. Used after
    a FLAC search came up empty and the user has explicitly opted in.
    """
    artist = track_meta.get("artist", "") or album_artist or ""
    title = track_meta.get("title", "")
    duration = int(track_meta.get("duration", 0) or 0)

    primary, fallbacks = _build_track_queries(artist, title)
    if not primary:
        return None, []

    seen_files: set[tuple[str, str]] = set()
    pooled: list[SearchResult] = []

    async def _run(q: str) -> None:
        responses = await slskd.search(q, timeout_secs=18, response_limit=180)
        parsed = slskd.parse_files(responses, lossless_only=not accept_lossy)
        for r in parsed:
            if accept_lossy and not _is_acceptable_lossy(r):
                continue
            key = (r.username, r.filename.lower())
            if key in seen_files:
                continue
            seen_files.add(key)
            pooled.append(r)

    def _scored() -> list[ScoredTrack]:
        return score_track_results(
            pooled,
            track_artist=artist,
            track_title=title,
            track_duration=duration,
            duration_tolerance=duration_tolerance,
        )

    # Tier 1 — primary alone. Stop here if we already have a confident pick.
    await _run(primary)
    scored = _scored()
    if scored and scored[0].score >= TRACK_AUTO_THRESHOLD:
        return scored[0], scored[1:5]

    # Tier 2 — fallbacks (title-only, ASCII-fold) sequentially with early exit.
    for q in fallbacks:
        await _run(q)
        if len(pooled) >= 60:
            break  # diminishing returns
        scored = _scored()
        if scored and scored[0].score >= TRACK_AUTO_THRESHOLD:
            break

    scored = _scored()
    if not scored:
        return None, []

    best = scored[0]
    alternatives = scored[1:5]
    if best.score >= TRACK_AUTO_THRESHOLD:
        return best, alternatives
    if best.score >= TRACK_PICK_THRESHOLD:
        return None, [best] + alternatives
    return None, []


async def find_tracks_concurrent(
    tracks: list[dict],
    album_artist: str,
    duration_tolerance: int = 5,
    concurrency: int = 2,
    stagger: float = 0.4,
) -> list[tuple[ScoredTrack | None, list[ScoredTrack]]]:
    """Run per-track searches concurrently with a small semaphore + stagger.

    slskd rate-limits ``POST /api/v0/searches`` (returns 429 above ~3 starts/s).
    Concurrency 2 with a 0.4s start stagger stays under the limit while still
    amortising network latency across the album.
    """
    sem = asyncio.Semaphore(concurrency)

    async def _one(idx: int, t: dict):
        await asyncio.sleep(idx * stagger)
        async with sem:
            return await find_track(t, album_artist=album_artist,
                                      duration_tolerance=duration_tolerance)

    return await asyncio.gather(*[_one(i, t) for i, t in enumerate(tracks)])
