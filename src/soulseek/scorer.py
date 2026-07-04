"""Score and rank slskd search results against a reference track or album.

Per-track scoring is two-axis (see ScoredTrack):
   match  (0..55) — identity: duration match (40, graduated tolerance) +
                    name relevance (15, artist/title word overlap across the
                    peer path), × version penalty. Thresholds
                    (soulseek.selection) act on this axis only.
   fetch (−6..35) — desirability: reliability (25: free slot, upload speed,
                    queue length) + quality (10 flat for lossless within the
                    cap, 5 lossy). Orders copies, never gates a match.
Ranking is (match, fetch); ``score`` keeps the legacy blend of both for
logs and the query-ladder early exit.

For album-folder matching: coverage 50 + quality 10 + reliability 25 +
filename 15 = 100, then × missing/version penalties.

Quality is *flat* across lossless formats — Bluetooth headphones can't
transmit above 16/48 (the codec downsamples), and ABX studies on hi-res vs
CD-quality consistently score at chance level for untrained listeners
(Meyer & Moran 2007 JAES, Reiss 2016 meta-analysis). Files exceeding
``MAX_BIT_DEPTH``/``MAX_SAMPLE_RATE_HZ`` are filtered out entirely so we
don't pull 2-3× larger files for no audible gain.

Reliability swings strongly with queue length and free-slot status — these
are the actual UX dominators (instant download vs. 10-minute wait) where
hi-res used to dominate.
"""

import logging
import re
from dataclasses import dataclass, field

from config import MAX_BIT_DEPTH, MAX_SAMPLE_RATE_HZ
from soulseek.client import SearchResult, PeerFolder
from soulseek.selection import group_copies

logger = logging.getLogger(__name__)


# Keywords that indicate an alternate version. Skipped if the source title
# doesn't already include them (i.e. user asked for the original, peer has a remix).
DIFFERENT_VERSION_KEYWORDS = (
    "remix", "rmx",
    "live", "live at", "live from", "live in", "live on",
    "acoustic", "unplugged",
    "slowed", "sped up", "speed up", "reverb",
    "radio edit", "radio version", "single edit", "album edit",
    "instrumental", "karaoke",
    "extended", "extended version", "extended mix",
    "demo", "rough cut", "rough mix",
    "cover", "tribute",
)

REMASTER_KEYWORDS = ("remaster", "remastered")


@dataclass
class ScoredTrack:
    result: SearchResult
    score: float               # legacy blend of both axes (logs + ladder exit)
    # Identity axis (0..55): duration + name evidence, ×version penalty —
    # "is this the right recording?" Thresholds act on this axis only.
    match_score: float = 0.0
    # Desirability axis (−6..35): reliability + quality — "which copy
    # first?" Orders candidates, never gates a match.
    fetch_score: float = 0.0
    # Every scored copy of this recording (identical basename on other
    # peers), best first — result is sources[0]. Transfer retries walk these
    # before falling to a different recording.
    sources: list[SearchResult] = field(default_factory=list)


@dataclass
class ScoredFolder:
    folder: PeerFolder
    score: float
    matched_files: list[SearchResult]   # files matched to expected track positions
    missing_count: int                  # tracks we couldn't find inside this folder


def _word_set(text: str) -> set[str]:
    return set(re.findall(r"\w+", (text or "").lower()))


def _filename_relevance(filename: str, artist: str, title: str) -> float:
    """0..15 points for artist/title word overlap with the filename."""
    fn_words = _word_set(filename)
    if not fn_words:
        return 0.0

    artist_words = _word_set(artist)
    title_words = _word_set(title)

    artist_match = (len(artist_words & fn_words) / len(artist_words)) if artist_words else 0.0
    title_match = (len(title_words & fn_words) / len(title_words)) if title_words else 0.0

    return artist_match * 7.5 + title_match * 7.5


def _path_relevance(directory: str, basename: str, artist: str, title: str) -> float:
    """0..15 points for artist/title appearing across the *full* peer path
    (directory + basename). Catches the common case where the directory has
    'Artist - Album' but the basename only has '01 Title.flac'.
    """
    dir_words = _word_set(directory)
    fn_words = _word_set(basename)
    combined = dir_words | fn_words
    if not combined:
        return 0.0

    artist_words = _word_set(artist)
    title_words = _word_set(title)

    artist_match = (len(artist_words & combined) / len(artist_words)) if artist_words else 0.0
    title_match = (len(title_words & fn_words) / len(title_words)) if title_words else 0.0

    return artist_match * 7.5 + title_match * 7.5


def _artist_in_path(directory: str, basename: str, artist: str) -> bool:
    """True if *any* meaningful artist word shows up in the path.

    Anti-falsepositive gate: a peer file with zero artist overlap is almost
    always the wrong recording (e.g. 'Barnacle' from a bird-sounds album
    matching a track called 'Barnacle' by an unrelated artist). When the
    artist's name has no word ≥3 chars (single letters, '21', etc.) we don't
    apply the gate — it would over-filter.
    """
    artist_words = _word_set(artist)
    significant = {w for w in artist_words if len(w) >= 3}
    if not significant:
        return True
    path_words = _word_set(directory) | _word_set(basename)
    return bool(significant & path_words)


def _duration_score(diff: int, tolerance: int = 5, hard_cutoff: int = 30) -> float | None:
    """Returns 0..40 or None if past hard cutoff (caller treats None as exclude)."""
    if diff <= tolerance:
        return 40.0 - diff * 2
    if diff <= 10:
        return 25.0 - (diff - tolerance) * 3
    if diff <= hard_cutoff:
        return max(0.0, 10.0 - (diff - 10) * 0.5)
    return None


def _exceeds_cap(bit_depth: int | None, sample_rate: int | None) -> bool:
    """True if peer's file is above the user-configured quality cap.

    None values (peer didn't report) are treated as "within cap" — many slskd
    peers omit these fields. Filtering on absence over-trims results.
    """
    if MAX_BIT_DEPTH and bit_depth and bit_depth > MAX_BIT_DEPTH:
        return True
    if MAX_SAMPLE_RATE_HZ and sample_rate and sample_rate > MAX_SAMPLE_RATE_HZ:
        return True
    return False


def _quality_score(bit_depth: int | None, sample_rate: int | None) -> float:
    """Flat 10-point reward for any lossless format within the cap.

    No hi-res bias: 24/96 scores the same as 16/44.1 — Bluetooth headphones
    can't transmit hi-res anyway, ABX studies score them at chance level.
    """
    if _exceeds_cap(bit_depth, sample_rate):
        return -1000.0
    return 10.0


def _quality_score_for_result(r: SearchResult) -> float:
    """Quality score that handles both lossless and lossy candidates.

    Lossless within cap: 10pt. Lossy (mp3/m4a, only reachable when accept_lossy
    is on): flat 5pt — the duration / filename / reliability signals decide
    ranking once we've already accepted the format compromise.
    """
    if r.is_lossless:
        return _quality_score(r.bit_depth, r.sample_rate)
    return 5.0


def _reliability_score(
    has_free_slot: bool, upload_speed: int, queue_length: int
) -> float:
    """0..25 reliability blend, swinging negative for very long queues.

    Scoring rationale: free slot = "download starts in seconds"; queue=0 with
    no free slot = "starts after current upload completes"; queue ≥ 15 = "10+
    minute wait, unreliable peer". Speed bonus matters but caps modestly so
    a perfect-queue 1MB/s peer can't be beaten by a queued 10MB/s peer.
    """
    score = 0.0
    if has_free_slot:
        score += 10.0
    # Upload speed: cap at 10 MB/s, 0.8 pt per MB/s up to ~8 points.
    if upload_speed > 0:
        score += min(upload_speed / 1_000_000, 10.0) * 0.8
    # Queue length: rewards instant access, penalises long waits.
    if queue_length == 0:
        score += 7.0
    elif queue_length <= 2:
        score += 3.0
    elif queue_length <= 5:
        score += 1.0
    elif queue_length <= 15:
        score -= 2.0
    else:
        # Past 15: linear penalty, cap at -6 so a single very-long-queue
        # peer doesn't hard-fail an otherwise great candidate.
        score -= min(queue_length / 5.0, 6.0)
    return score


def _version_penalty(filename_lower: str, source_title_lower: str) -> float:
    """Multiplicative penalty for mismatched alternate versions.

    Returns 1.0 if no mismatch detected, 0.75 for remaster, 0.30 if the result
    is clearly a live/remix/acoustic/etc and the source isn't.
    """
    for kw in DIFFERENT_VERSION_KEYWORDS:
        if kw in filename_lower and kw not in source_title_lower:
            return 0.30
    for kw in REMASTER_KEYWORDS:
        if kw in filename_lower and kw not in source_title_lower:
            return 0.75
    return 1.0


def score_track_results(
    results: list[SearchResult],
    track_artist: str,
    track_title: str,
    track_duration: int,
    duration_tolerance: int = 5,
    hard_cutoff: int = 30,
) -> list[ScoredTrack]:
    """Score and rank candidates for a single track. Excluded items are dropped.

    Copies of the same recording (identical basename on several peers) are
    grouped: one ranking entry, all copies kept in its ``sources``.
    """
    title_lower = (track_title or "").lower()
    out: list[ScoredTrack] = []

    for r in results:
        fname_lower = r.basename.lower()
        dir_lower = r.directory.lower()

        # Duration: exclude if outside hard cutoff
        if r.length is not None and track_duration:
            d_score = _duration_score(abs(r.length - track_duration),
                                       tolerance=duration_tolerance,
                                       hard_cutoff=hard_cutoff)
            if d_score is None:
                continue
        else:
            # Unreported length is weakly consistent with the target (80% of
            # a perfect match), not punished — name + artist evidence must
            # still carry the identity, but it *can* carry it: a full name
            # match reaches confident territory instead of being capped
            # below every threshold forever.
            d_score = 32.0

        # Hard anti-falsepositive: drop candidates where the artist name
        # appears nowhere in the peer's directory or filename. A title like
        # "Barnacle" otherwise matches a bird-sounds album from a totally
        # unrelated artist if duration happens to align.
        if not _artist_in_path(dir_lower, fname_lower, track_artist):
            continue

        # Quality cap (MAX_BIT_DEPTH / MAX_SAMPLE_RATE_HZ). Skip peers above.
        if _exceeds_cap(r.bit_depth, r.sample_rate):
            continue

        name = _path_relevance(dir_lower, fname_lower, track_artist, track_title)
        vp = _version_penalty(fname_lower, title_lower)
        fetch = _quality_score_for_result(r) \
            + _reliability_score(r.has_free_slot, r.upload_speed, r.queue_length)

        out.append(ScoredTrack(
            result=r,
            score=round((d_score + name + fetch) * vp, 2),
            match_score=round((d_score + name) * vp, 2),
            fetch_score=round(fetch, 2),
        ))

    # Identity first, peer desirability second: a correct file from a slow
    # peer outranks a wrong-ish file from a fast one — retries and
    # same-recording sources deal with slow peers, nothing deals with
    # having downloaded the wrong recording.
    out.sort(key=lambda x: (x.match_score, x.fetch_score), reverse=True)

    # One ranking entry per recording; other peers' copies stay reachable
    # as sources for transfer retries.
    return group_copies(out)


_LEADING_NUM_RE = re.compile(r"^\s*(\d{1,3})\b")


def _leading_number(basename: str) -> int | None:
    """Track number a peer put at the start of the filename, if any."""
    m = _LEADING_NUM_RE.match(basename)
    return int(m.group(1)) if m else None


def _match_folder_to_tracks(
    folder: PeerFolder,
    track_durations: list[int],
    track_titles: list[str],
    duration_tolerance: int = 5,
) -> tuple[list[SearchResult | None], int]:
    """Match files inside a folder to the expected track list.

    Duration stays the eligibility gate (±tolerance), but assignment is by a
    joint pair score — duration closeness, title-word overlap, and a leading
    track-number hint — instead of closest-duration-first. A full title match
    is worth a few seconds of duration edge; the old greedy let a 1s-closer
    wrong file beat an exactly-titled right one, silently pairing the wrong
    audio with a track's tags.

    Returns ``(matched_files, missing_count)`` where ``matched_files`` is a
    sparse list **parallel to track_durations** — ``None`` at positions where
    no file matched. Callers ``zip(album["tracks"], matched_files)`` to align
    file → track; the position-preserving shape is essential when the folder
    is missing one or more tracks (otherwise the surviving files shift left
    and the wrong track number gets tagged with the wrong audio).
    """
    available = [f for f in folder.files if f.is_lossless]
    if not available:
        available = list(folder.files)

    # Score every eligible (track, file) pair, then assign globally best
    # pairs first — so a strong title claim on one track can't be stolen by
    # an earlier track with a marginally closer duration.
    pairs: list[tuple[float, int, int, int]] = []
    for i, want_dur in enumerate(track_durations):
        title_words = _word_set(track_titles[i] if i < len(track_titles) else "")
        for j, f in enumerate(available):
            if f.length is None:
                continue
            diff = abs(f.length - want_dur)
            if diff > duration_tolerance:
                continue
            title_ratio = (len(title_words & _word_set(f.basename)) / len(title_words)) \
                if title_words else 0.0
            number_hint = 1.0 if _leading_number(f.basename) == i + 1 else 0.0
            score = -diff + 3.0 * title_ratio + number_hint
            pairs.append((score, diff, i, j))

    pairs.sort(key=lambda p: (-p[0], p[1], p[2], p[3]))
    matched: list[SearchResult | None] = [None] * len(track_durations)
    used: set[int] = set()
    for _score, _diff, i, j in pairs:
        if matched[i] is not None or j in used:
            continue
        matched[i] = available[j]
        used.add(j)

    missing = sum(1 for m in matched if m is None)
    return matched, missing


def score_folder_results(
    folders: list[PeerFolder],
    track_durations: list[int],
    track_titles: list[str],
    album_artist: str,
    album_title: str,
    duration_tolerance: int = 5,
) -> list[ScoredFolder]:
    """Score whole peer folders as album-bundle candidates.

    Penalises missing tracks heavily, rewards lossless and reliability.
    """
    n_expected = len(track_durations)
    if n_expected == 0:
        return []

    out: list[ScoredFolder] = []

    for folder in folders:
        # Same anti-falsepositive gate the per-track scorer applies: a folder
        # whose full path and file names share no significant artist word is
        # almost never the right album, however well durations line up.
        # Title-only fallback queries otherwise let sound-alike folders from
        # unrelated artists into a phase that never faces the track thresholds.
        all_basenames = " ".join(f.basename for f in folder.files)
        if not _artist_in_path(folder.directory, all_basenames, album_artist):
            continue

        matched, missing = _match_folder_to_tracks(
            folder, track_durations, track_titles,
            duration_tolerance=duration_tolerance,
        )
        # `matched` is parallel to track_durations with None for missing tracks.
        # Filter to concrete files for averaging / cap-checking.
        concrete = [f for f in matched if f is not None]
        if not concrete:
            continue

        # Quality cap on the *folder* — if any matched file exceeds the cap,
        # treat the whole folder as too-high-quality (we'd otherwise download
        # mixed-spec files which Navidrome groups awkwardly).
        if any(_exceeds_cap(f.bit_depth, f.sample_rate) for f in concrete):
            continue

        # Coverage score (0..50)
        coverage = (len(concrete) / n_expected) * 50.0

        # Average per-file quality (0..10 flat for lossless)
        bd_avg = sum((f.bit_depth or 16) for f in concrete) / len(concrete)
        sr_avg = sum((f.sample_rate or 44100) for f in concrete) / len(concrete)
        quality = _quality_score(int(bd_avg), int(sr_avg))

        # Reliability (0..20)
        reliability = _reliability_score(
            folder.has_free_slot, folder.upload_speed, folder.queue_length,
        )

        # Filename relevance (0..15) — average across matched files
        fname_rel = sum(
            _filename_relevance(f.basename.lower(), album_artist, album_title)
            for f in concrete
        ) / len(concrete)

        # Heavy penalty for any missing track in album bundle (we don't want
        # to download a folder missing track 7 if a complete folder exists).
        # Slope 2.0 so 50% missing → 0 score floor, encouraging *complete*
        # peer folders over partial ones even if those have nice reliability.
        missing_penalty = 1.0 - (missing / n_expected) * 2.0
        missing_penalty = max(missing_penalty, 0.05)

        # Version penalty: take worst-case per filename vs respective track
        # title. Iterate the SPARSE list so each file is paired with the
        # right track title (skip None positions where no file was found).
        worst_version_penalty = 1.0
        for i, f in enumerate(matched):
            if f is None:
                continue
            t = track_titles[i] if i < len(track_titles) else album_title
            p = _version_penalty(f.basename.lower(), (t or "").lower())
            worst_version_penalty = min(worst_version_penalty, p)

        score = (coverage + quality + reliability + fname_rel) \
                * missing_penalty * worst_version_penalty

        out.append(ScoredFolder(
            folder=folder,
            score=round(score, 2),
            matched_files=matched,
            missing_count=missing,
        ))

    out.sort(key=lambda x: x.score, reverse=True)
    return out
