"""Candidate selection: group identical recordings across peers.

Ranking wants one entry per *recording*; transfer retries want every peer's
copy of that recording. The old dedupe-by-basename served the first need by
destroying the second — the same rip circulating under an identical filename
on many peers is exactly the redundancy a failed transfer falls back on.

Grouping serves both: each surviving ScoredTrack keeps ``sources`` — every
copy that passed scoring, best first — and the download order produced by
``flatten_candidates`` exhausts the best recording's copies before falling
to the next recording.

This module holds selection *decisions* over already-scored candidates; it
deliberately imports nothing from the scorer (candidates are duck-typed
``.result``/``.sources`` carriers) so scoring math can depend on it.
"""

from soulseek.client import SearchResult

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
