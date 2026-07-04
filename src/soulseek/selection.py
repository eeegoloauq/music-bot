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
