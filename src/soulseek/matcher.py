"""High-level matching: turn a Tidal album/track into the best slskd choice.

Two-stage strategy for albums:

  1. Album-folder search (``"Artist Album"``) — try to find a single peer who
     owns the whole album. If we find one with high confidence, use it;
     downloading from one peer is faster (single connection, single queue) and
     the metadata is more consistent.

  2. Per-track fallback — if no peer has the full album, iterate per track and
     pick the best match each time.

For single-track downloads we go directly to per-track search.
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
ALBUM_AUTO_THRESHOLD = 70.0
ALBUM_PICK_THRESHOLD = 50.0


def _strip_diacritics(s: str) -> str:
    """Remove combining marks; keeps the base letters intact."""
    nf = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in nf if not unicodedata.combining(c))


_PARENS_VERSION = re.compile(
    r"\s*[\(\[](?:explicit|clean|deluxe|remaster(?:ed)?|expanded|anniversary|"
    r"special|bonus[^)\]]*|.*\bversion\b[^)\]]*)[\)\]]",
    re.IGNORECASE,
)


def _clean_query(text: str) -> str:
    """Strip noise that wouldn't help a Soulseek text search."""
    text = _PARENS_VERSION.sub("", text or "")
    text = _strip_diacritics(text)
    # Drop punctuation slskd treats poorly; keep $ for stylized names like A$AP.
    text = re.sub(r"[\\/:*?\"<>|\[\]\(\)]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _build_album_queries(artist: str, album: str) -> list[str]:
    a = _clean_query(artist)
    b = _clean_query(album)
    queries: list[str] = []
    if a and b:
        queries.append(f"{a} {b}")
        if a.lower() != b.lower():
            queries.append(b)
    elif b:
        queries.append(b)
    elif a:
        queries.append(a)
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        ql = q.lower()
        if ql in seen:
            continue
        seen.add(ql)
        out.append(q)
    return out


def _build_track_queries(artist: str, title: str) -> list[str]:
    a = _clean_query(artist)
    t = _clean_query(title)
    out: list[str] = []
    if a and t:
        out.append(f"{a} {t}")
    if t:
        out.append(t)
    return out


# --- album matching ---------------------------------------------------------

async def find_album(
    album_meta: dict,
    duration_tolerance: int = 5,
) -> tuple[ScoredFolder | None, list[ScoredFolder]]:
    """Search for a peer-folder that holds the whole album.

    Returns ``(best, alternatives)``. ``best`` is set when its score is past
    ``ALBUM_AUTO_THRESHOLD``; below that the caller should look at alternatives
    (or fall back to per-track).
    """
    artist = album_meta.get("artist", "")
    title = album_meta.get("title", "")
    tracks = album_meta.get("tracks", [])
    track_durations = [int(t.get("duration", 0) or 0) for t in tracks]
    track_titles = [t.get("title", "") for t in tracks]

    queries = _build_album_queries(artist, title)
    all_folders: dict[tuple[str, str], slskd.PeerFolder] = {}

    for q in queries:
        responses = await slskd.search(q, timeout_secs=20, response_limit=200)
        results = slskd.parse_files(responses, lossless_only=True)
        for f in slskd.group_by_folder(results):
            key = (f.username, f.directory)
            if key not in all_folders:
                all_folders[key] = f

    if not all_folders:
        return None, []

    scored = score_folder_results(
        list(all_folders.values()),
        track_durations=track_durations,
        track_titles=track_titles,
        album_artist=artist,
        album_title=title,
        duration_tolerance=duration_tolerance,
    )

    if not scored:
        return None, []

    best = scored[0]
    alternatives = scored[1:6]   # cap returned alternatives to keep UI manageable

    # Complete folder always wins, regardless of nominal score. If a single
    # peer has every track of the album with matching durations, that beats
    # any per-track Frankenstein assembly (avoids cross-version mixes,
    # downloads in one queue position, fewer flaky connections).
    if best.missing_count == 0:
        return best, alternatives

    if best.score >= ALBUM_AUTO_THRESHOLD:
        return best, alternatives
    if best.score >= ALBUM_PICK_THRESHOLD:
        return None, [best] + alternatives
    return None, []


# --- per-track matching ----------------------------------------------------

async def find_track(
    track_meta: dict,
    album_artist: str | None = None,
    duration_tolerance: int = 5,
) -> tuple[ScoredTrack | None, list[ScoredTrack]]:
    """Search and score for a single track. Returns ``(auto_pick, alternatives)``.

    ``auto_pick`` is filled in when score >= TRACK_AUTO_THRESHOLD; otherwise the
    caller can use the ``alternatives`` list (still already filtered & sorted).
    """
    artist = track_meta.get("artist", "") or album_artist or ""
    title = track_meta.get("title", "")
    duration = int(track_meta.get("duration", 0) or 0)

    queries = _build_track_queries(artist, title)
    if not queries:
        return None, []

    seen_files: set[tuple[str, str]] = set()
    pooled: list[SearchResult] = []
    for q in queries:
        responses = await slskd.search(q, timeout_secs=18, response_limit=180)
        for r in slskd.parse_files(responses, lossless_only=True):
            key = (r.username, r.filename.lower())
            if key in seen_files:
                continue
            seen_files.add(key)
            pooled.append(r)
        if len(pooled) >= 60:
            break  # diminishing returns from extra queries

    scored = score_track_results(
        pooled,
        track_artist=artist,
        track_title=title,
        track_duration=duration,
        duration_tolerance=duration_tolerance,
    )

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
