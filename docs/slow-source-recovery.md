# Slow-source recovery

Design note (signed off 2026-07-21; open questions resolved below) for three
fixes motivated by the
2026-07-20 incidents. Companion to docs/source-selection.md, which describes
how a source gets *chosen*; this note is about noticing, mid-album, that the
chosen source is bad in a way selection couldn't see, and about two smaller
robustness holes the same night exposed.

## The incidents (anonymized)

**Slow-source lock-in.** A 13-track album: search returned 279 files from 19
peers; the winning folder peer (call it peer A) covered 13/13 at the locked
quality with fine *advertised* metrics — score 87.0, free slot, decent
reported upload speed. Actual throughput was ~0.1 MB/s on every track;
12 tracks downloaded sequentially over ~1 hour. Track 1 had failed early
('Completed, TimedOut') and was fetched at the very end from folder fallback
peer B at 2.2 MB/s in 9.3 s — a fast, same-quality (16-bit/44kHz)
alternative existed the whole time, already ranked in the folder chain. For
contrast, a healthy album earlier the same day ran 1.7–3.5 MB/s per track
from its folder peer.

Today the downloader reconsiders sources only on *hard failure*
(`PeerTransferError` → next candidate; `PEER_ABANDON_K` consecutive failures
→ next folder). Measured speed is never an input: a peer that delivers
every byte at 0.1 MB/s is indistinguishable from a great one. The
`fetch_score` reliability terms (`scorer._reliability_score`) are
advertisements from search time; peer A's were simply wrong.

**Fragile status edits.** An upload import was killed by a transient
`telegram.error.NetworkError` raised from the *cosmetic*
`status_msg.edit_text("Importing upload…")` inside the critical `try` in
`bot._handle_upload` — a ~30 s network blip aborted the import before
`import_staged_album` ever ran. Staging was orphaned, the user got a
confusing report, and the follow-up album request searched Soulseek for all
11 tracks including the 6 sitting staged. A messaging hiccup must never
abort filing work.

**Runner disk.** Each CI build leaves a ~320 MB SHA-tagged image on the
runner's 10 G root disk. The lala repo already hit ENOSPC from this and
grew a cleanup step; this repo has none.

## A. Slow-source recovery in the album downloader

Guiding principle (owner's): **switching must be earned, not dogmatic**. If
staying on the current peer is the best available option — no eligible
fallback at the same locked quality and coverage — staying IS the correct
outcome and stays first-class. The mechanism below is a recovery path for
the case where a demonstrably better option is already in hand, not a speed
optimizer.

### What "persistently slow" means

Measurement uses **completed transfers only**, from the per-attempt numbers
`downloader._attempt` already computes for its log line (`size / elapsed`,
enqueue-to-done — queue wait counts, because it's part of the delivered
rate the user experiences). No mid-transfer sampling in v1: reacting to
instantaneous `speed_bps` from `wait_for_files` means killing in-flight
transfers on noise; reacting between tracks bounds the loss to ~one track.

A completed track is a **qualifying sample** iff it transferred at least
`SLOW_TRACK_MIN_BYTES` (5 MB constant, selection.py) — below that,
interludes finish inside TCP slow-start and the average is noise.

A peer is **persistently slow** after `SLOW_STREAK_K = 2` *consecutive*
qualifying tracks each below the floor `SLOW_SOURCE_MIN_MBPS` (env knob,
default **0.5 MB/s**, read in config.py like the quality caps). A fast
qualifying track resets the streak; failures don't feed the streak (the
failure logic owns those); non-qualifying tracks neither count nor reset.

Why 0.5: the incident peer ran 0.075–0.1 MB/s (5–7× below), healthy peers
the same day never dipped under 1.7; at 0.5 a typical 30–50 MB redbook FLAC
still lands in ≤100 s, so nothing tolerable gets flagged. It's an absolute
floor, not relative-to-advertised — advertised numbers are exactly what the
incident proved untrustworthy. Note the hole this fills precisely: at
0.1 MB/s a 20 MB track finishes in ~200 s, far under
`DOWNLOAD_TIMEOUT_SECS` (900), so the existing timeout never fires — slow
peers *complete* everything, they just eat an hour doing it.

### When switching is earned

On a persistently-slow verdict mid-folder-walk, a replacement must satisfy
all of:

- **Same quality lock.** `modal_quality(candidate.matched_files)` must
  match the album's `quality_lock` (None components wildcard, as in
  `pick_quality_locked`). Never trade the lock for speed.
- **Covers all remaining tracks.** Checked via the candidate folder's
  `matched_files` (parallel to `album["tracks"]`) against
  `remaining_track_ids`. Whole-folder replacement, not per-track scatter —
  argued below.
- **Not already disqualified.** Not abandoned for failures this album, and
  not a peer already *measured* slower-or-equal to the current one (see
  anti-thrash).

**Whole-folder over per-track fallbacks:** the ranked folder chain
(`plan_folder_phase`'s `FolderPlan.chain`) already exists, is already
quality-scored, and preserves the one-peer/one-queue-slot/consistent-rip
property the folder phase exists for. Per-track re-matching would scatter a
*working* album across peers to fix a speed problem — Frankenstein assembly
is the fallback of last resort, not a speed remedy. The switch target is
the first chain entry (rank order) meeting the conditions; entries skipped
over stay in the chain as ordinary failure fallbacks behind it.

**Freshness:** no re-search. Queue/slot info in the chain is from search
time and by switch time is minutes stale — accepted, because searches cost
10 s+ under pacing (soulseek/client.py) and the same measurement discipline
applies to the switched-to peer: a bad switch self-corrects within
`SLOW_STREAK_K` tracks. The switch itself is the freshness probe.

If no chain entry qualifies, **stay** — logged as a first-class decision
(below), evaluated once: a "stayed" verdict marks the walk settled (the
chain can't improve mid-album), so it doesn't re-log every track.

The "covers ALL remaining tracks" bar is deliberately conservative. If real
use shows switches rarely fire because chain folders have partial coverage,
the pre-approved next step is relaxing it to "covers all but ≤1 remaining,
leftover goes to gap-fill" — a v1.1 change, not part of this series.

### Anti-thrash

- `MAX_SLOW_SWITCHES = 2` per album (selection.py constant). Not
  configurable — it's a safety bound, not a preference.
- The switched-to peer faces the exact same measurement. Streaks are
  per-peer; a switch starts the new peer at zero.
- A per-peer running average over qualifying tracks is kept for the album.
  Switching **to** a peer whose measured average is ≤ the current peer's is
  forbidden — so after A→B, a return to A is allowed only if A measured
  faster than B is measuring, and the cap ends it there: the album settles
  on the better of two slow peers, and A→B→A→B is impossible (cap) and
  pointless (rule).
- Slowness is **per-album** state, in memory only. No cross-album peer
  reputation in v1 (open question below).

### Interaction with existing fallback + resume

- **Failure fallback unchanged.** `PEER_ABANDON_K` counting,
  `failed_candidate_keys`, and per-track fallback keep their semantics. A
  slow peer is *demoted, not failed*: its (peer, file) keys do NOT enter
  `failed_candidate_keys` — its files remain legitimate last-resort retry
  candidates. In the per-track gap-fill phase, candidates from
  measured-slow peers are moved to the back of `order_for_download`'s list
  (still eligible — better a slow track than a missing one), so gap-fill
  doesn't immediately reward the peer we just walked away from.
- **Resume journal untouched.** The journal stores intent (album id), not
  transfer state (docs/download-resilience.md); the monitor is in-memory
  and resets on resume. A resumed album re-searches, may re-pick the same
  slow peer, and re-detects within 2 tracks — no schema change, no
  regression to attach-first enqueue (an attached live transfer completes
  and simply becomes the next sample).

### Decision logging

Exactly one INFO line per decision, both outcomes:

```
Slow source: peer A avg 0.09 MB/s over 2 track(s) (floor 0.5) — switching to peer B (covers 9/9 remaining @ 16-bit/44kHz, chain rank 1, switch 1/2)
Slow source: peer A avg 0.09 MB/s over 2 track(s) (floor 0.5) — staying: no chain folder covers 9 remaining @ 16-bit/44kHz
```

### Where it hooks in

| seam | change |
|------|--------|
| `selection.py` | new policy: `SlowSourceMonitor` (record samples, per-peer averages, streaks, switch budget, verdicts) + eligibility check over the chain; constants `SLOW_STREAK_K`, `SLOW_TRACK_MIN_BYTES`, `MAX_SLOW_SWITCHES`. All policy stays in selection, per the F6 rule |
| `config.py` | `SLOW_SOURCE_MIN_MBPS` env knob (default 0.5; `0` disables the whole mechanism) |
| `downloader.download_album` folder-chain loop | after each successful `_attempt`, feed the monitor; a switch verdict breaks to the chosen chain entry exactly like the existing `PEER_ABANDON_K` break (reusing the folder-fallback `emit`/log path, which already handles mid-album peer changes in the UI) |
| `downloader._attempt` | expose `(peer, bytes, elapsed)` it already computes for its log line |
| per-track fallback phase | demote measured-slow peers' candidates to the back of the flattened list |

`scorer.py` is untouched — advertised reliability keeps doing its job at
selection time; measurement is a runtime concern.

## B. Status-edit robustness

One helper in bot.py, `safe_edit(status_msg, text, **kwargs)` (and a
matching `safe_send` for initial status messages): retry transient
`telegram.error.NetworkError` / `TimedOut` with short backoff (3 attempts,
~2/5/10 s — sized to outlive a blip like the incident's), honor
`RetryAfter.retry_after` once, then **log WARNING and continue** — it never
raises. Non-final status edits are cosmetic; pipeline-critical steps must
never be aborted by messaging failures.

Applies to every non-final `status_msg.edit_text` / initial `reply_text`:
`_handle_upload` (the incident site — the "Importing upload…" edit inside
the critical `try` before `import_staged_album`), `_do_download_album`,
`_do_download_track`, and the retag flows. `LiveStatus.refresh` already
swallows `TelegramError` and stays as is. If the *initial* send fails after
retries, the flow proceeds with a null message whose edits no-op — the
download/import is the job, the UI is best-effort.

**Final result messages** retry harder (5 attempts, longer backoff), but a
total send failure still must not lose completed work: `_send_result`
already swallows `TelegramError`, and filing/scan-trigger/journal semantics
are unchanged — a lost final message is logged at ERROR, the files stay
filed. Test approach: a fake bot/message whose `edit_text` raises
`NetworkError` N times then succeeds (helper retries, N+1 calls) or always
raises (helper returns False, flow completes); an upload-driver test
asserting `import_staged_album` runs and files despite a raising edit.

## C. CI hygiene

Add a "Drop superseded local images" step to the `build` job of
`.forgejo/workflows/deploy.yml`, same pattern as
/opt/lala/.forgejo/workflows/deploy.yml: `if: always()`, list local
`homelab/music-bot:*` tags, `grep -v` the just-built SHA, `docker rmi` the
rest, then `docker image prune -f`. The just-built image stays as layer
cache; older tags live in the registry. Without it the 10 G runner disk
gains ~320 MB per push until builds die with ENOSPC (lala, 2026-07-05).

## Characterization tests (before any behavior change)

New `tests/test_slow_source_characterization.py` + one upload test, pinning
today's semantics so each fix flips exactly one:

1. **Speed-blind folder walk**: an all-tracks-succeed folder peer is never
   abandoned regardless of (stubbed) per-track duration — album completes
   with single-peer provenance. *Flips with the slow-source commit.*
2. **Failed-track ordering**: a track that fails on the primary folder is
   retried from the next chain folder only after the primary's walk
   completes (the incident's track 1 finishing last).
3. **`PEER_ABANDON_K`**: two consecutive failures abandon the folder; a
   success in between resets the counter.
4. **Quality lock**: `order_for_download` promotes the lock-matching
   candidate; `pick_quality_locked` falls back to `picks[0]` when nothing
   matches.
5. **`failed_candidate_keys` crosses phases**: a (peer, file) that failed
   in the folder phase is not retried per-track.
6. **Upload abort-on-edit** (pins the bug): `edit_text` raising
   `NetworkError` aborts `_handle_upload` before `import_staged_album`.
   *Flips with the safe-edit commit.*

## Commit series (one fix per diff, on dev)

1. this design note
2. characterization tests (no src changes)
3. safe-edit helper + non-final call sites (flips test 6, adds its own)
4. selection.py slow-source policy + unit tests (monitor, eligibility,
   anti-thrash — pure policy, not yet wired)
5. downloader wiring + decision logging (flips test 1)
6. CI: drop superseded local images

Observe 4–5 on the dev deployment against real albums before promoting to
main.

## Resolved (2026-07-21)

1. **Floor stays 0.5 MB/s default**, hi-res cap included. The mechanism
   targets pathological peers (the incident ran 5–7× below healthy); 1.0
   would start flagging acceptable ones. The env knob covers taste.
2. **Slowness stays per-album, in memory.** A session-scoped peer
   reputation risks stale penalties and adds state, for a re-detection
   that costs at most `SLOW_STREAK_K` tracks. Possible future step, not v1.
3. **Between-tracks-only accepted for v1.** Roadmap item —
   **mid-transfer minimum-progress check**: a very large single track
   (200 MB at 0.1 MB/s) still burns up to `DOWNLOAD_TIMEOUT_SECS`, because
   v1 only measures completed transfers; a minimum-bytes-per-interval check
   during the transfer would bound that too.
4. **Upload-staging retry on startup: backlog**, out of scope for this
   series. With safe_edit the crash cause is gone, and staging survives
   for a manual re-drop.
