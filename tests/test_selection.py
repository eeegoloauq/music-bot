"""Unit behavior of ``soulseek.selection``: recording grouping, the download
order it produces, and the folder-phase plan."""

from soulseek.client import PeerFolder
from soulseek.scorer import ScoredFolder, ScoredTrack
from soulseek.selection import (
    FOLDER_COVERAGE_MIN,
    flatten_candidates,
    group_copies,
    order_for_download,
    plan_folder_phase,
)

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


def test_group_copies_splits_incompatible_lengths():
    # Same basename, clearly different length = a different edit, not a
    # retry copy; unknown length never piggybacks on a known-length group.
    album = ScoredTrack(result=make_result("a", "M\\X\\Artist - Song.flac",
                                           length=200), score=90.0)
    near = ScoredTrack(result=make_result("b", "N\\Y\\Artist - Song.flac",
                                          length=208), score=70.0)
    radio = ScoredTrack(result=make_result("c", "O\\Z\\Artist - Song.flac",
                                           length=180), score=40.0)
    unknown = ScoredTrack(result=make_result("d", "P\\W\\Artist - Song.flac",
                                             length=None), score=47.0)
    grouped = group_copies([album, near, radio, unknown])
    assert [g.result.username for g in grouped] == ["a", "c", "d"]
    assert [r.username for r in grouped[0].sources] == ["a", "b"]  # 208 joins 200
    assert len(grouped[1].sources) == 1  # 180 is its own recording
    assert len(grouped[2].sources) == 1  # unknown length stands alone


def test_flatten_falls_back_to_result_when_ungrouped():
    # ScoredTracks that never went through group_copies have empty sources.
    st = _scored("a", "M\\X\\01.flac", 60.0)
    assert flatten_candidates(st, []) == [st.result]


def test_order_for_download_promotes_quality_locked_pick():
    hires = _scored("a", "M\\X\\01 - Song.flac", 90.0)
    hires.result.bit_depth, hires.result.sample_rate = 24, 96_000
    cd = _scored("b", "M\\Y\\01 - Song (cd).flac", 80.0)

    ordered = order_for_download(hires, [cd], quality_lock=(16, 44100))
    assert ordered == [cd.result, hires.result]  # lock match first, rest kept

    assert order_for_download(hires, [cd]) == [hires.result, cd.result]
    assert order_for_download(None, [], quality_lock=(16, 44100)) == []


def _scored_folder(missing_count, n_matched):
    files = [make_result("peer", f"M\\Artist - Album\\{i:02d}.flac")
             for i in range(n_matched)]
    folder = PeerFolder(username="peer", directory="M\\Artist - Album",
                        files=files)
    return ScoredFolder(folder=folder, score=80.0, matched_files=files,
                        missing_count=missing_count)


def test_plan_folder_phase_coverage_boundary():
    complete = _scored_folder(missing_count=0, n_matched=4)
    alt = _scored_folder(missing_count=1, n_matched=3)

    plan = plan_folder_phase(complete, [alt], n_tracks=4)
    assert plan is not None
    assert plan.chain == [complete, alt]
    assert plan.quality_lock == (16, 44100)  # modal quality of matched files

    # exactly at the boundary (3/4 = FOLDER_COVERAGE_MIN) still plans
    assert FOLDER_COVERAGE_MIN == 0.75
    assert plan_folder_phase(_scored_folder(1, 3), [], n_tracks=4) is not None
    # below it: straight to per-track
    assert plan_folder_phase(_scored_folder(2, 2), [], n_tracks=4) is None
    assert plan_folder_phase(None, [], n_tracks=4) is None
    assert plan_folder_phase(complete, [], n_tracks=0) is None
