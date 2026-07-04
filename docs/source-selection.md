# Source selection

How the bot decides *what* to download once metadata is resolved: which peer,
which folder, which file, in what order to retry. The second half documents
the 2026-07 redesign — what was wrong before and what deliberately changed —
because several current behaviors only make sense against that history.

## The pipeline

```
metadata (Deezer canonical album/track JSON)
  │
  ▼
matcher.find_album / find_track      build search queries, escalate a query
  │                                  ladder, collect results into a pool
  ▼
scorer.score_folder_results /        score peer folders (albums) and files
scorer.score_track_results           (tracks) against the reference metadata
  │
  ▼
selection                            every *decision*: thresholds, folder-vs-
  │                                  per-track plan, quality lock, recording
  │                                  grouping, candidate order, lossy gate
  ▼
downloader.download_album /          execute: enqueue on slskd, wait, retry
downloader.download_single_track     failed peers, move into library, tag
```

Two strategies for albums:

1. **Album-folder first** — find one peer whose single directory covers the
   whole album. One connection, one queue slot, consistent rip.
2. **Per-track fallback** — for whatever the folder didn't cover, match each
   track independently, assembling across peers ("Frankenstein" mode), with a
   quality lock keeping the result uniform where possible.

Single tracks skip strategy 1.

### The results pool

Every search an album triggers feeds one shared pool. `find_album` returns it
(`pool`), and the per-track fallback passes it to `find_track` as `preseed` —
a confident match from the pool costs zero new searches, and once searching
has proven throttled, `allow_search=False` keeps matching the remaining
tracks against the pool without touching the network. Folder sightings merge
across the query ladder: different queries legitimately surface different
subsets of one peer folder (Soulseek matches terms against full paths), so
file lists union per (peer, directory) and the newest response's queue/slot
stats win.

## Scoring

Per-track scoring (`scorer.score_track_results`) is **two-axis**:

| axis | range | components | role |
|------|-------|------------|------|
| `match_score` | 0..55 | duration match (40, graduated: −2/s to ±5s, −3/s to ±10s, →0 at ±30s, excluded past; 32 when the peer didn't report length) + name relevance (15, artist/title word overlap across the peer path), × version penalty (×0.30 live/remix/acoustic…, ×0.75 remaster, when the reference title lacks the keyword) | "Is this the right recording?" — the only axis thresholds act on |
| `fetch_score` | −6..35 | reliability (25: free slot +10, speed ≤+8, queue +7…−6) + quality (10 flat lossless within cap, 5 lossy) | "Which copy first?" — orders candidates and sources, never gates a match |

Ranking is `(match, fetch)`: a correct file from a queued slow peer outranks
a wrong-ish file from a fast one — retries and same-recording sources deal
with slow peers; nothing deals with having downloaded the wrong recording.
`score` keeps the legacy blend of both axes for logs and one ladder exit.

Two hard gates run before scoring: the **artist gate** (a candidate whose
full path shares no significant artist word is dropped — anti-falsepositive)
and the **quality cap** (`MAX_BIT_DEPTH`/`MAX_SAMPLE_RATE_HZ`; deliberate:
no hi-res bias, files above the cap are excluded entirely).

**Recording grouping** (`selection.group_copies`): same-basename copies on
different peers are one ranking entry holding every copy in `sources`, best
fetch first. The download order (`selection.order_for_download`) exhausts
the best recording's copies before falling to a different recording, and
promotes the quality-locked pick to the front when a lock is in force.

**Per-folder** (`scorer.score_folder_results`), max 100: coverage 50 +
quality 10 + reliability 25 + name 15, × missing-track penalty (slope 2.0 —
50% missing floors the score) × worst per-file version penalty. Folders face
the same artist gate as tracks (over the directory plus all basenames).
Files are assigned to expected track positions by a **joint pair score** —
duration closeness (eligibility stays ±5s), title-word overlap (worth ~3
seconds of duration edge), and a leading track-number hint — assigned
globally best-pair first, so an exact title can't be stolen by a marginally
closer duration.

## Where decisions happen

All selection policy lives in `soulseek/selection.py`:

| constant / function | value | what it does |
|---------------------|-------|--------------|
| `MATCH_FLOOR` | 20 | identity floor — below this the file isn't credibly the same recording (an exact-duration live take scores 16.5) |
| `SEARCH_SATISFIED_MATCH` | 45 | stop escalating the query ladder: identity is confident (perfect-duration + half a name = 47.5; full name + unknown duration = 47) |
| `SEARCH_SATISFIED_BLEND` | 70 | legacy blended exit kept alongside, so ladder exits are a strict superset of the old behavior — search volume can only decrease; removable after observation |
| `FOLDER_COVERAGE_MIN` | 0.75 | `plan_folder_phase`: below this coverage, skip folders entirely and go per-track |
| `PEER_ABANDON_K` | 2 | consecutive failures before a folder peer is abandoned for the next chain entry |
| `MP3_MIN_KBPS` | 256 | lossy-fallback floor, in the protocol's units (slskd's `bitRate` is kbps) |
| `plan_folder_phase()` | — | folder chain + modal quality lock, or None → per-track |
| `order_for_download()` | — | (auto, alternatives, lock) → the candidate list every download path walks |

Outside selection: query ladders and the 60-result pool cap in `matcher`,
`MAX_BIT_DEPTH` / `MAX_SAMPLE_RATE_HZ` / `MAX_FILE_BYTES` in `config`,
transfer timeouts in `downloader`.

## Design decisions worth knowing

- **Pool over re-searching.** Searches cost 10s+ each under pacing (see
  `soulseek/client.py`); every layer prefers matching against already-
  gathered results. Honest errors: a search that *couldn't run* raises
  `SearchError`/`SearchThrottledError` — `[]` always means "ran, nothing".
- **Folder-first with ranked fallback chain**, cross-phase `failed_keys` so
  retries go to other peers, and the modal **quality lock** so gap-filling a
  partial folder doesn't produce a mixed-spec album (lock misses fall back
  to the best candidate — better a mismatched track than a missing one).
- **m4a has no bitrate floor** in the lossy gate: it might be ALAC and we
  can't tell without parsing the stream. Unreported mp3 bitrate is rejected.
- **There is no picker UI.** `find_track`'s `(auto, alternatives)` split
  only affects candidate order; both download paths flatten and download
  the best eligible candidate.

## The 2026-07 redesign

The current shape came from a reviewed redesign (one failure mode per
commit, each pinned by a characterization test in
`tests/test_selection_characterization.py` that flipped in the fixing
commit). What was wrong, and what deliberately changed:

| | was | now |
|--|-----|-----|
| **F1** | dedupe-by-basename deleted exactly the same-recording-other-peer copies that transfer retries need | copies group into `sources`; retries exhaust the best recording's peers before trying a different recording |
| **F2** | `find_album` kept the first sighting of a folder wholesale; fallback queries' fuller file lists were dropped | folder sightings union across the ladder; genuinely complete folders win the folder phase |
| **F3** | one scalar blended identity with peer desirability; thresholds (named for a picker that doesn't exist) acted on the blend; unreported duration was capped below "confident" forever | two axes; thresholds act on match only; correct-but-slow outranks wrong-ish-but-fast; full-name unknown-duration hits count as confident |
| **F4** | folder files assigned closest-duration-first — similar-length tracks swapped files, wrong audio got the right tag; no artist gate for folders | joint (duration, title, track-number) pair scoring; folders face the track scorer's artist gate |
| **F5** | mp3 floor of 256_000 compared bps against slskd's kbps field — every mp3 failed, the lossy fallback was m4a-only | `MP3_MIN_KBPS = 256`, confirmed against the live daemon |
| **F6** | policy scattered across scorer/matcher/downloader, candidate flattening duplicated three times | all policy in `selection.py`; one flattening path |

Intentional behavior deltas beyond the fixes: the identity floor retires
what the old blended floor let fast peers sneak in (≥6s-off files with zero
title overlap), version-mismatched files stay excluded as before, and
unrelated-artist folders are no longer eligible however well their durations
line up.

## Out of scope

The search pacing/throttle layer (`soulseek/client.py`), tagging and library
layout, Telegram UX, and the audio-source contract itself
(`download_album` / `download_single_track`) — everything upstream stays
source-agnostic.
