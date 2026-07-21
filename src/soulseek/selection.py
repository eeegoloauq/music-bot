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


# Same basename but a clearly different reported length is a different edit
# (radio cut vs album version among loose 'Artist - Song.flac' files), not a
# retry-equivalent copy — downloading it under the ranked pick's identity
# would tag the wrong audio. VBR length estimates jitter a few seconds;
# real edits differ by 15s+.
SAME_RECORDING_MAX_LENGTH_DIFF_SECS = 10


def _same_recording(a: SearchResult, b: SearchResult) -> bool:
    if a.length is None or b.length is None:
        return a.length is None and b.length is None
    return abs(a.length - b.length) <= SAME_RECORDING_MAX_LENGTH_DIFF_SECS


def group_copies(scored: list) -> list:
    """Collapse a best-first ScoredTrack list to one entry per recording.

    Identity is the basename plus a compatible reported length (see
    ``_same_recording``). The surviving entry is the best-scored copy; every
    compatible copy (including the survivor) lands in its ``sources``,
    preserving score order, for transfer retries. Incompatible same-name
    files stay separate ranking entries and stand on their own scores.
    """
    groups: dict[str, list] = {}
    out: list = []
    for st in scored:
        key = st.result.basename.lower()
        kept = next((g for g in groups.setdefault(key, [])
                     if _same_recording(g.result, st.result)), None)
        if kept is None:
            st.sources = [st.result]
            groups[key].append(st)
            out.append(st)
        else:
            kept.sources.append(st.result)
    return out


# --- slow-source recovery (docs/slow-source-recovery.md §A) -----------------
# Advertised peer metrics are search-time claims; measured delivery is the
# truth. A peer that completes every track at 0.1 MB/s never trips the
# transfer timeout — this policy notices it between tracks and decides
# whether a switch to a chain fallback is *earned* (same quality lock, full
# remaining coverage). If staying is the best available option, staying is
# the correct outcome, not a failure.

# A completed track is a qualifying speed sample only above this size —
# smaller files finish inside TCP slow-start and the average is noise.
SLOW_TRACK_MIN_BYTES = 5 * 1024 * 1024

# Consecutive qualifying tracks below the floor before a peer counts as
# persistently slow. A fast qualifying track resets the streak; failures and
# sub-minimum tracks neither count nor reset.
SLOW_STREAK_K = 2

# Hard safety bound on switches per album — not a preference knob: together
# with the measured-average rule it guarantees the walk settles on the
# better of two slow peers instead of thrashing between them.
MAX_SLOW_SWITCHES = 2


class SlowSourceMonitor:
    """Per-album, in-memory measured-speed bookkeeping: qualifying samples,
    per-peer streaks and running averages, the switch budget, and the
    settled flag. A floor of 0 disables the whole mechanism."""

    def __init__(self, floor_mbps: float):
        self.floor_mbps = floor_mbps
        self.switches_used = 0
        self.settled = False              # a "stay" verdict ends evaluation
        self._sums: dict[str, tuple[float, int]] = {}   # peer -> (Σ MB/s, n)
        self._streak: dict[str, int] = {}
        self._flagged: set[str] = set()   # peers ever verdicted slow (sticky)

    @property
    def enabled(self) -> bool:
        return self.floor_mbps > 0

    def record(self, peer: str, size_bytes: int, elapsed_secs: float) -> None:
        """Feed one *completed* transfer (enqueue-to-done). Failures never
        come here — the failure logic owns those."""
        if not self.enabled or size_bytes < SLOW_TRACK_MIN_BYTES or elapsed_secs <= 0:
            return
        mbps = (size_bytes / (1024 * 1024)) / elapsed_secs
        total, n = self._sums.get(peer, (0.0, 0))
        self._sums[peer] = (total + mbps, n + 1)
        if mbps < self.floor_mbps:
            self._streak[peer] = self._streak.get(peer, 0) + 1
            if self._streak[peer] >= SLOW_STREAK_K:
                self._flagged.add(peer)
        else:
            self._streak[peer] = 0

    def sample_count(self, peer: str) -> int:
        return self._sums.get(peer, (0.0, 0))[1]

    def avg_mbps(self, peer: str) -> float | None:
        total, n = self._sums.get(peer, (0.0, 0))
        return total / n if n else None

    def is_measured_slow(self, peer: str) -> bool:
        """Ever hit a persistently-slow verdict this album. Sticky — a later
        fast track resets the streak but not the gap-fill demotion."""
        return peer in self._flagged

    def wants_switch(self, peer: str) -> bool:
        """Should the downloader evaluate a switch away from ``peer`` now?"""
        return (self.enabled and not self.settled
                and self.switches_used < MAX_SLOW_SWITCHES
                and self._streak.get(peer, 0) >= SLOW_STREAK_K)

    def note_switch(self) -> None:
        self.switches_used += 1

    def settle(self) -> None:
        """A "stay" verdict: the chain can't improve mid-album, so don't
        re-evaluate (or re-log) on every further slow track."""
        self.settled = True


def _quality_lock_matches(candidate_quality, quality_lock) -> bool:
    """Same wildcard semantics as ``pick_quality_locked``: a None lock
    component matches anything on that axis; never trade the lock for speed."""
    if not quality_lock or quality_lock == (None, None):
        return True
    cand_bd, cand_sr = candidate_quality
    bd, sr = quality_lock
    return (bd is None or cand_bd == bd) and (sr is None or cand_sr == sr)


def find_slow_source_switch(monitor: SlowSourceMonitor, current_peer: str,
                            chain, album_tracks, remaining_track_ids,
                            quality_lock, abandoned_peers=frozenset(),
                            ):
    """First chain entry (rank order) that's an *earned* switch target away
    from ``current_peer``: same quality lock, covers every remaining track,
    not failure-abandoned this album, and not measured slower-or-equal to
    the current peer. Whole-folder replacement only — per-track scatter is
    the fallback of last resort, not a speed remedy.

    Returns ``(chain_rank, folder)`` or ``None`` (= staying is correct).
    """
    current_avg = monitor.avg_mbps(current_peer)
    for rank, cand in enumerate(chain):
        peer = cand.folder.username
        if peer == current_peer or peer in abandoned_peers:
            continue
        if not _quality_lock_matches(modal_quality(cand.matched_files),
                                     quality_lock):
            continue
        covered = {t["id"] for t, f in zip(album_tracks, cand.matched_files)
                   if f is not None}
        if not remaining_track_ids <= covered:
            continue
        cand_avg = monitor.avg_mbps(peer)
        if cand_avg is not None and current_avg is not None \
                and cand_avg <= current_avg:
            continue  # anti-thrash: only switch to a measured-faster peer
        return rank, cand
    return None


def demote_measured_slow(candidates: list[SearchResult],
                         monitor: SlowSourceMonitor) -> list[SearchResult]:
    """Stable reorder of a per-track candidate list: files from peers this
    album measured slow move to the back — still eligible (better a slow
    track than a missing one), but gap-fill shouldn't immediately reward the
    peer the folder walk just left."""
    slow = [c for c in candidates if monitor.is_measured_slow(c.username)]
    if not slow:
        return candidates
    return [c for c in candidates
            if not monitor.is_measured_slow(c.username)] + slow


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
