"""Unit behavior of ``soulseek.selection``: recording grouping and the
download order it produces."""

from soulseek.scorer import ScoredTrack
from soulseek.selection import flatten_candidates, group_copies

from conftest import make_result


def _scored(username, filename, score):
    return ScoredTrack(result=make_result(username, filename), score=score)


def test_group_copies_keeps_ranking_and_collects_sources():
    scored = [
        _scored("a", "M\\X\\01 - Song.flac", 90.0),
        _scored("b", "M\\Y\\02 - Other.flac", 80.0),
        _scored("c", "N\\Z\\01 - Song.flac", 72.0),  # same recording as first
    ]
    grouped = group_copies(scored)
    assert [g.result.username for g in grouped] == ["a", "b"]  # order unchanged
    assert [r.username for r in grouped[0].sources] == ["a", "c"]  # best first
    assert [r.username for r in grouped[1].sources] == ["b"]


def test_flatten_exhausts_best_recording_before_next():
    scored = [
        _scored("a", "M\\X\\01 - Song.flac", 90.0),
        _scored("b", "M\\Y\\02 - Other.flac", 80.0),
        _scored("c", "N\\Z\\01 - Song.flac", 72.0),
    ]
    auto, *alts = group_copies(scored)
    order = flatten_candidates(auto, alts)
    assert [(r.username, r.basename) for r in order] == [
        ("a", "01 - Song.flac"),
        ("c", "01 - Song.flac"),   # same recording, next peer — before...
        ("b", "02 - Other.flac"),  # ...a different recording
    ]


def test_flatten_handles_missing_auto_and_dedups():
    st = _scored("a", "M\\X\\01.flac", 60.0)
    st.sources = [st.result]
    assert flatten_candidates(None, [st, st]) == [st.result]
    assert flatten_candidates(None, []) == []


def test_flatten_falls_back_to_result_when_ungrouped():
    # ScoredTracks that never went through group_copies have empty sources.
    st = _scored("a", "M\\X\\01.flac", 60.0)
    assert flatten_candidates(st, []) == [st.result]
