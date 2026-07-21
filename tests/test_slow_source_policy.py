"""Slow-source recovery policy (docs/slow-source-recovery.md §A): the
monitor's sampling/streak/average rules, switch eligibility over the folder
chain, and the anti-thrash guarantees. Pure policy — no downloader here."""

import types

from soulseek.selection import (
    MAX_SLOW_SWITCHES,
    SLOW_TRACK_MIN_BYTES,
    SlowSourceMonitor,
    demote_measured_slow,
    find_slow_source_switch,
)

from conftest import make_result

MB = 1024 * 1024


def record_track(mon, peer, mbps, size=20 * MB):
    mon.record(peer, size, (size / MB) / mbps)


# --- monitor ----------------------------------------------------------------

def test_two_slow_qualifying_tracks_trip_the_verdict():
    mon = SlowSourceMonitor(0.5)
    record_track(mon, "a", 0.1)
    assert not mon.wants_switch("a")  # one slow track is noise
    record_track(mon, "a", 0.08)
    assert mon.wants_switch("a")
    assert mon.is_measured_slow("a")
    assert mon.avg_mbps("a") == 0.09


def test_fast_qualifying_track_resets_the_streak():
    mon = SlowSourceMonitor(0.5)
    record_track(mon, "a", 0.1)
    record_track(mon, "a", 2.0)
    record_track(mon, "a", 0.1)
    assert not mon.wants_switch("a")
    assert not mon.is_measured_slow("a")


def test_small_tracks_neither_count_nor_reset():
    mon = SlowSourceMonitor(0.5)
    record_track(mon, "a", 0.1)
    # An interlude below the sample minimum, even at a "fast" rate: ignored.
    mon.record("a", SLOW_TRACK_MIN_BYTES - 1, 0.5)
    assert mon.sample_count("a") == 1
    record_track(mon, "a", 0.1)
    assert mon.wants_switch("a")  # the streak survived the interlude


def test_flag_is_sticky_but_streak_is_not():
    mon = SlowSourceMonitor(0.5)
    record_track(mon, "a", 0.1)
    record_track(mon, "a", 0.1)
    record_track(mon, "a", 2.0)
    assert not mon.wants_switch("a")   # streak reset — no switch pressure
    assert mon.is_measured_slow("a")   # but gap-fill demotion remembers


def test_floor_zero_disables_everything():
    mon = SlowSourceMonitor(0.0)
    record_track(mon, "a", 0.01)
    record_track(mon, "a", 0.01)
    assert not mon.wants_switch("a")
    assert not mon.is_measured_slow("a")
    assert mon.sample_count("a") == 0


def test_switch_budget_and_settled_gate_the_verdict():
    mon = SlowSourceMonitor(0.5)
    record_track(mon, "a", 0.1)
    record_track(mon, "a", 0.1)
    for _ in range(MAX_SLOW_SWITCHES):
        assert mon.wants_switch("a")
        mon.note_switch()
    assert not mon.wants_switch("a")  # budget exhausted — safety bound

    mon2 = SlowSourceMonitor(0.5)
    record_track(mon2, "a", 0.1)
    record_track(mon2, "a", 0.1)
    mon2.settle()
    assert not mon2.wants_switch("a")  # "stayed" verdict is final


# --- switch eligibility -----------------------------------------------------

TRACKS = [{"id": str(i)} for i in range(1, 4)]


def chain_entry(username, covered_ids, bit_depth=16, sample_rate=44100):
    files = [make_result(username, f"M\\A\\{t['id']}.flac",
                         bit_depth=bit_depth, sample_rate=sample_rate)
             if t["id"] in covered_ids else None
             for t in TRACKS]
    return types.SimpleNamespace(
        folder=types.SimpleNamespace(username=username),
        score=70.0, missing_count=files.count(None), matched_files=files)


def slow_monitor(peer="cur"):
    mon = SlowSourceMonitor(0.5)
    record_track(mon, peer, 0.1)
    record_track(mon, peer, 0.08)
    return mon


def test_first_eligible_chain_entry_wins():
    chain = [chain_entry("cur", {"1", "2", "3"}),
             chain_entry("partial", {"2"}),          # doesn't cover remaining
             chain_entry("good", {"1", "2", "3"}),
             chain_entry("later", {"1", "2", "3"})]  # eligible but outranked
    got = find_slow_source_switch(slow_monitor(), "cur", chain, TRACKS,
                                  {"2", "3"}, (16, 44100))
    assert got is not None
    rank, folder = got
    assert (rank, folder.folder.username) == (2, "good")


def test_quality_lock_never_traded_for_speed():
    chain = [chain_entry("cur", {"1", "2", "3"}),
             chain_entry("hires", {"1", "2", "3"}, bit_depth=24,
                         sample_rate=96_000)]
    assert find_slow_source_switch(slow_monitor(), "cur", chain, TRACKS,
                                   {"2", "3"}, (16, 44100)) is None


def test_none_lock_components_wildcard():
    chain = [chain_entry("cur", {"1", "2", "3"}),
             chain_entry("hires", {"1", "2", "3"}, bit_depth=24,
                         sample_rate=96_000)]
    got = find_slow_source_switch(slow_monitor(), "cur", chain, TRACKS,
                                  {"2", "3"}, (None, None))
    assert got is not None and got[1].folder.username == "hires"
    # one wildcard axis: bit depth must still match
    assert find_slow_source_switch(slow_monitor(), "cur", chain, TRACKS,
                                   {"2", "3"}, (16, None)) is None


def test_abandoned_peers_are_not_switch_targets():
    chain = [chain_entry("cur", {"1", "2", "3"}),
             chain_entry("broken", {"1", "2", "3"})]
    assert find_slow_source_switch(slow_monitor(), "cur", chain, TRACKS,
                                   {"2", "3"}, (16, 44100),
                                   abandoned_peers={"broken"}) is None


def test_never_switch_to_measured_slower_or_equal_peer():
    # After cur→other, coming back to cur is allowed only if cur measured
    # faster than other is measuring — ≤ is forbidden, so A→B→A→B can't spin.
    mon = slow_monitor("cur")            # cur avg 0.09
    record_track(mon, "other", 0.05)
    record_track(mon, "other", 0.05)     # other avg 0.05 ≤ 0.09
    chain = [chain_entry("cur", {"1", "2", "3"}),
             chain_entry("other", {"1", "2", "3"})]
    assert find_slow_source_switch(mon, "cur", chain, TRACKS,
                                   {"2", "3"}, (16, 44100)) is None

    # ...but a measured-faster previous peer is a legitimate back-switch.
    got = find_slow_source_switch(mon, "other", chain, TRACKS,
                                  {"2", "3"}, (16, 44100))
    assert got is not None and got[1].folder.username == "cur"


def test_unmeasured_chain_peer_is_eligible():
    got = find_slow_source_switch(slow_monitor(), "cur",
                                  [chain_entry("cur", {"1", "2", "3"}),
                                   chain_entry("fresh", {"1", "2", "3"})],
                                  TRACKS, {"2", "3"}, (16, 44100))
    assert got is not None and got[1].folder.username == "fresh"


# --- gap-fill demotion ------------------------------------------------------

def test_demote_measured_slow_is_stable():
    mon = slow_monitor("slowpeer")
    a = make_result("slowpeer", "M\\A\\1.flac")
    b = make_result("fresh1", "M\\A\\1.flac")
    c = make_result("fresh2", "S\\A\\1.flac")
    assert demote_measured_slow([a, b, c], mon) == [b, c, a]
    assert demote_measured_slow([b, c], mon) == [b, c]
