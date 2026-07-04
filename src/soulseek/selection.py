"""Selection policy: every decision about *which* source gets downloaded.

The scorer produces facts (match/fetch scores, folder coverage); the
matcher gathers candidates; the downloader executes transfers. This module
holds what sits between: thresholds, the folder-vs-per-track plan, quality
locking, the lossy-fallback gate, and candidate grouping/ordering.

Grouping: ranking wants one entry per *recording*; transfer retries want
every peer's copy of that recording. The old dedupe-by-basename served the
first need by destroying the second — the same rip circulating under an
identical filename on many peers is exactly the redundancy a failed
transfer falls back on. Each surviving ScoredTrack keeps ``sources`` (every
copy that passed scoring, best first), and the download order exhausts the
best recording's copies before falling to the next recording.

This module deliberately imports nothing from the scorer (candidates are
duck-typed ``.result``/``.sources``/``.missing_count`` carriers) so scoring
math can depend on it.
"""

import logging
from dataclasses import dataclass

from soulseek.client import SearchResult

logger = logging.getLogger(__name__)

# Identity floor (match axis, 0..55): below this the file isn't credibly the
# same recording, whatever the peer looks like. 20 keeps everything with real
# name+duration evidence (unknown duration + half a name = 39.5) while
# retiring what the old blended floor let fast peers sneak in: version
# mismatches (an exact-duration live take = 16.5) and ≥6s-off files with no
# title overlap.
MATCH_FLOOR = 20.0

# Stop escalating the query ladder once the top candidate's identity is this
# confident: a perfect-duration half-name hit (47.5) or a full-name
# unknown-duration hit (47) both qualify. More searching can only find
# faster copies, and same-recording sources already handle slow peers.
SEARCH_SATISFIED_MATCH = 45.0

# Legacy blended exit, kept alongside the match-axis exit so ladder exits
# are a strict superset of the old behavior — search volume (10s+ per query
# under pacing) can only decrease. Candidates here are slightly-off-duration
# or partial-name hits from ideal peers; probably right, instantly
# downloadable, not worth another 20s of searching. Removable after
# observation.
SEARCH_SATISFIED_BLEND = 70.0


def search_satisfied(best) -> bool:
    """True when the top candidate is good enough to stop searching for."""
    return (best.match_score >= SEARCH_SATISFIED_MATCH
            or best.score >= SEARCH_SATISFIED_BLEND)


# Mp3-fallback gates. Triggered only when accept_lossy=True (after a FLAC
# search came up empty). The floor keeps 128kbps junk off the disk; m4a has
# no floor because it might be ALAC (lossless) — we can't tell without
# parsing the stream. slskd passes through the Soulseek protocol's bitrate
# attribute, which is in kbps (320 for CBR mp3) — verified live.
LOSSY_FALLBACK_EXTS = {"mp3", "m4a"}
MP3_MIN_KBPS = 256


def is_acceptable_lossy(r: SearchResult) -> bool:
    if r.extension not in LOSSY_FALLBACK_EXTS:
        return False
    if r.extension == "mp3" and (r.bit_rate or 0) < MP3_MIN_KBPS:
        return False
    return True


# Folder-phase policy:
#   complete coverage: prefer the folder whole — Frankenstein assemblies are
#     strictly worse than a single peer (one queue slot, consistent rip).
#   partial ≥ FOLDER_COVERAGE_MIN: use what the folder covers, gap-fill the
#     rest per-track under the folder's modal quality lock.
#   below: skip folders, full per-track — the fallback's job is to pick a
#     coherent assembly across peers.
FOLDER_COVERAGE_MIN = 0.75

# K consecutive failures from one folder peer → that peer is systemically
# broken (their slskd queue / share index / DB); abandon it for the next
# folder-rank alternative. K=1 over-reacts to single transient rejections;
# K=3+ wastes minutes per dead peer.
PEER_ABANDON_K = 2


@dataclass
class FolderPlan:
    """How the album's folder phase runs: ranked peer-folder chain (best
    first) and the quality lock gap-fill has to respect."""
    chain: list
    quality_lock: tuple[int | None, int | None] | None


def plan_folder_phase(folder_match, folder_alternatives, n_tracks) -> FolderPlan | None:
    """Decide whether the album runs a folder phase at all, and with what.

    ``None`` means straight to per-track matching. ``folder_match`` /
    ``folder_alternatives`` are ScoredFolders from the matcher (duck-typed).
    """
    if not folder_match or not n_tracks:
        return None
    coverage = (n_tracks - folder_match.missing_count) / n_tracks
    if coverage < FOLDER_COVERAGE_MIN:
        return None
    return FolderPlan(
        chain=[folder_match, *(folder_alternatives or [])],
        quality_lock=modal_quality(folder_match.matched_files),
    )


def modal_quality(matched_files) -> tuple[int | None, int | None]:
    """Pick the dominant (bit_depth, sample_rate) across a folder's matched
    files, ignoring None positions and unreported quality. Used as the
    quality-lock target so per-track gap-fill stays uniform with the folder."""
    counts: dict[tuple[int | None, int | None], int] = {}
    for f in matched_files or []:
        if f is None:
            continue
        key = (f.bit_depth, f.sample_rate)
        if key == (None, None):
            continue
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return (None, None)
    return max(counts.items(), key=lambda kv: kv[1])[0]


def pick_quality_locked(picks: list[SearchResult], quality_lock):
    """From a candidate list, return the first one matching the (bd, sr) lock.
    Falls back to the first candidate if none match — better a mismatched
    track than a missing one. Quality-lock with None components matches
    anything for that axis."""
    if not picks:
        return None
    if not quality_lock or quality_lock == (None, None):
        return picks[0]
    target_bd, target_sr = quality_lock
    for p in picks:
        if (target_bd is None or p.bit_depth == target_bd) and \
           (target_sr is None or p.sample_rate == target_sr):
            return p
    chosen = picks[0]
    logger.warning(
        "Quality-lock %s not satisfied; falling back to %s/%s — album may end up mixed-quality",
        quality_label(quality_lock),
        f"{chosen.bit_depth}-bit" if chosen.bit_depth else "?",
        f"{chosen.sample_rate // 1000}kHz" if chosen.sample_rate else "?",
    )
    return chosen


def quality_label(qlock) -> str:
    if not qlock:
        return "any quality"
    bd, sr = qlock
    parts = []
    if bd:
        parts.append(f"{bd}-bit")
    if sr:
        parts.append(f"{sr // 1000}kHz")
    return "/".join(parts) if parts else "any quality"


def order_for_download(auto, alternatives, quality_lock=None) -> list[SearchResult]:
    """The candidate order the downloader walks for one track: flattened
    recording-then-copies order, with the quality-locked pick promoted to
    the front when a lock is in force."""
    picks = flatten_candidates(auto, alternatives)
    if not picks or not quality_lock:
        return picks
    chosen = pick_quality_locked(picks, quality_lock)
    return [chosen] + [r for r in picks if r is not chosen]


def group_copies(scored: list) -> list:
    """Collapse a best-first ScoredTrack list to one entry per recording.

    Identity is the basename — the same key the old dedupe used, so ranking
    is unchanged. The surviving entry is the best-scored copy; every copy
    (including the survivor) lands in its ``sources``, preserving score
    order, for transfer retries.
    """
    groups: dict[str, object] = {}
    out: list = []
    for st in scored:
        key = st.result.basename.lower()
        kept = groups.get(key)
        if kept is None:
            st.sources = [st.result]
            groups[key] = st
            out.append(st)
        else:
            kept.sources.append(st.result)
    return out


def flatten_candidates(auto, alternatives) -> list[SearchResult]:
    """Ranked download order for a track: every copy of the best recording,
    then the next recording's copies, deduped by (peer, path).

    This is the one place the auto/alternatives split flattens into the
    candidate list the downloader walks on transfer failures.
    """
    seen: set[tuple[str, str]] = set()
    out: list[SearchResult] = []
    for st in (auto, *alternatives):
        if st is None:
            continue
        for r in st.sources or [st.result]:
            key = (r.username, r.filename)
            if key not in seen:
                seen.add(key)
                out.append(r)
    return out
