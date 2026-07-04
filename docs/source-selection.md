# Source selection

How the bot decides *what* to download once metadata is resolved: which peer,
which folder, which file, in what order to retry. This document describes the
system as of v2.2, names its concrete failure modes, and proposes a redesign.
The first half is current-state documentation; everything under
[Proposed redesign](#proposed-redesign) is a plan, not shipped behavior.

## The pipeline

```
metadata (Deezer canonical album/track JSON)
  │
  ▼
matcher.find_album / find_track      build search queries, escalate a query
  │                                  ladder, collect results into a pool
  ▼
scorer.score_folder_results /        rank peer folders (albums) and files
scorer.score_track_results           (tracks) against the reference metadata
  │
  ▼
downloader.download_album /          decide folder-vs-per-track, walk ranked
downloader.download_single_track     candidates, retry failed peers, enforce
  │                                  quality uniformity
  ▼
slskd enqueue → wait → move into library → tag
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
tracks against the pool without touching the network. This layer is recent
(added with the search-pacing work) and is the pattern the rest of the system
should converge on: results gathered once, reused everywhere, network touched
only when the pool can't answer.

## Scoring today

Two scoring families in `soulseek/scorer.py`, both producing a single scalar.

**Per-track** (`score_track_results`), max ≈ 90:

| component  | points | signal |
|------------|--------|--------|
| duration   | 40 (12 if peer didn't report length) | graduated: −2/s to ±5s, −3/s to ±10s, →0 at ±30s, excluded past ±30s |
| quality    | 10 lossless flat, 5 lossy | no hi-res bias; over-cap files excluded |
| reliability| 25 | free slot +10, speed ≤+8, queue +7 … −6 |
| name match | 15 | artist/title word overlap with dir + basename |
| version    | ×0.30 / ×0.75 | live/remix/acoustic… vs remaster, when the reference title lacks the keyword |

Plus a hard **artist gate**: a candidate whose full path shares no significant
word with the artist name is dropped outright. Results are then deduped by
basename, keeping the highest-scored copy.

**Per-folder** (`score_folder_results`), max 100: coverage 50 + quality 10 +
reliability 25 + name 15, multiplied by a missing-track penalty (slope 2.0 —
50% missing floors the score) and the worst per-file version penalty. Files
are assigned to expected track positions by closest duration (±5s), with
title-word overlap only breaking exact ties.

### Where decisions actually happen

| constant | value | lives in | what it really does |
|----------|-------|----------|---------------------|
| `TRACK_AUTO_THRESHOLD` | 70 | matcher | stop escalating the query ladder; label the top result "auto" |
| `TRACK_PICK_THRESHOLD` | 45 | matcher | floor — below it a track reports "no match" |
| pooled-results cap | 60 | matcher | stop tier-2 fallback queries early |
| `PARTIAL_COVERAGE_MIN` | 0.75 | downloader | use a partial folder + gap-fill vs full per-track |
| `PEER_ABANDON_K` | 2 | downloader | consecutive failures before abandoning a folder peer |
| `max_attempts` | 3 (single) / all (album) | downloader | peers tried per track before giving up |
| `_LOSSY_MIN_MP3_BITRATE` | 256_000 | matcher | mp3-fallback quality floor |
| `MAX_BIT_DEPTH` / `MAX_SAMPLE_RATE_HZ` | 24 / 96000 | config → scorer | hard cap, filters hi-res above it |
| `MAX_FILE_BYTES` | 2 GiB | config → client | drops absurd single files at parse time |

## What works — keep it

- The **pool/preseed layer**: zero-search matching, pool-only degraded mode,
  honest `SearchError`/`SearchThrottledError` propagation (never `[]` for
  "couldn't run").
- **Folder-first with ranked fallback chain** and cross-phase
  `failed_keys` so retries go to *other* peers.
- **Quality cap and flat lossless scoring** — deliberate, documented choices.
- The **artist gate** for per-track false positives.
- **Modal quality lock** for gap-filling a partial folder.

## Failure modes

### F1 — basename dedupe defeats cross-peer retry

`score_track_results` dedupes candidates by basename, keeping the
highest-scored copy. But the most common redundancy on Soulseek is the *same
rip* on many peers — identical basenames. Those duplicates are exactly what
the retry path needs: when the chosen peer rejects the enqueue or stalls,
`_try_candidates` walks the remaining list, which now contains only
*differently named* files — often alternate versions — instead of the same
file from the next peer. Concrete: peer A wins with `01 - Song.flac` (score
84), errors on transfer; peer B's identical `01 - Song.flac` (score 72) was
deduped away; the bot retries a `Song (Live)` variant at ×0.30 or fails the
track.

### F2 — folder file-lists don't merge across the query ladder

`find_album` collects folders via `all_folders.setdefault((user, dir), f)` —
the first sighting of a folder wins **wholesale**. Soulseek matches search
terms against full paths, so a title-only fallback query legitimately
surfaces *more files from the same folder* than the primary did (e.g. the
directory carries the album name while only some basenames carry the artist).
Those extra files are silently dropped. Consequences: coverage undercounted →
a genuinely complete folder scores as partial → unnecessary per-track
searches; and the shared pool is thinner than what the searches actually
returned. The stale first-seen copy also keeps outdated queue/slot stats.

### F3 — one scalar, two meanings; thresholds that don't do what they say

The track score sums "is this the right recording" (duration 40 + name 15 +
version penalty) with "is this peer pleasant to download from" (reliability
25 + quality 10). Fallout:

- A peer that doesn't report duration caps at **62** (12 neutral + 10 + 25 +
  15) — permanently below `TRACK_AUTO_THRESHOLD` 70, no matter how exact the
  name match or how good the peer.
- A *correct* file from a queued, slow peer can rank below a *wrong-ish*
  file from a fast one.
- The AUTO/PICK split promises "download vs show picker", but there is no
  picker: both download paths flatten `auto` + `alternatives` into one
  candidate list and download the best entry regardless. In practice 70 only
  means "stop searching more query tiers" and 45 means "report no match
  below this". The names describe a UI that doesn't exist.

### F4 — greedy duration-first folder assignment; no artist gate for folders

`_match_folder_to_tracks` gives each expected track the file with the
closest duration; title overlap only breaks *exact* ties. Two tracks of
similar length can swap files — and since tags are force-written from Deezer
metadata afterwards, the wrong audio silently gets the right tag. A 1-second
duration edge beats an exact title match. Separately, the per-track scorer's
artist gate has no folder-level counterpart: with title-only fallback
queries, a folder of coincidentally similar durations from an unrelated
artist can win the folder phase, which never faces the 45/70 thresholds at
all — only the coverage check.

### F5 — the mp3 floor compares bps against a kbps field

The Soulseek protocol reports file bitrate in **kbps** (320, 256, ~245 for
V0) and slskd passes it through as `bitRate`. `_is_acceptable_lossy` rejects
mp3 below `_LOSSY_MIN_MP3_BITRATE = 256_000` — so every mp3 on the network
fails the floor (320 < 256 000), as does unreported bitrate (`None → 0`).
Net effect: the "no FLAC found → offer lossy fallback" flow can only ever
offer `.m4a`. Needs a one-search confirmation against live slskd before the
fix lands, but the fix is the same either way: express the floor in kbps and
tolerate both scales.

### F6 — selection policy is scattered

Weights live in `scorer`, thresholds and query ladders and lossy gates in
`matcher`, coverage/abandon/quality-lock in `downloader`, and the
`auto`+`alternatives` → candidate-list flattening is duplicated three times
(`find_lossy_candidates`, `download_single_track`, the album fallback). No
single place answers "why was this source chosen", and no change to policy
is local to one file.

## Proposed redesign

Finish what the pool started: results are gathered once into a first-class
candidate pool, selection decisions are pure functions over that pool, and
the downloader just executes plans.

### 1. Candidate pool with source-groups (fixes F1, F2)

New module `soulseek/selection.py`:

- `PeerSource` — one peer's copy of a file: username, remote path, size,
  quality fields, slot/speed/queue.
- `TrackCandidate` — one *recording*: identity key ≈ (normalized basename,
  duration bucket), holding `sources: [PeerSource]` ranked by fetch
  preference.
- `FolderCandidate` — (user, dir) with its files; file-lists **merge** across
  queries (union of files, freshest peer stats win).

Deduplication becomes *grouping*: ranking still shows one entry per
recording, but the sources stay available for retries — same file, next
peer, before falling to the next recording.

### 2. Two scoring axes (fixes F3)

- `match_score` — "is this the right recording": duration + name + artist
  gate + version penalty. The only score thresholds apply to.
- `fetch_score` — "which copy first": slot, speed, queue, within-cap
  quality. Orders sources inside a candidate, never gates a match.

Ranking: filter by match floor, sort by (match, best fetch). A missing
duration degrades match confidence gracefully (name signals can still carry
it to a confident match) instead of imposing a hard sub-auto ceiling.
Threshold names say what they do: `SEARCH_SATISFIED` (stop escalating
queries), `MATCH_FLOOR` (below = not the same recording).

### 3. Joint folder assignment + folder artist gate (fixes F4)

Score each (track, file) pair jointly — duration closeness, title overlap,
track-number hint from the basename — and assign by pair score, so an exact
title beats a 1-second duration edge. Folders pass the same artist gate as
single tracks.

### 4. One policy module (fixes F5, F6)

All selection constants and decision functions move to `selection.py`:
`plan_album(pool, album_meta) → AlbumPlan`, `plan_track(pool, track_meta) →
TrackPlan` — pure, synchronous, unit-testable. `matcher` keeps query
building and search orchestration; `downloader` executes plans and feeds
failures back (a failed source is marked dead in the pool, the next plan
skips it). The mp3 floor becomes `MP3_MIN_KBPS = 256`, in protocol units.

### Behavior contract

- Everything that downloads today still downloads. A characterization suite
  (`tests/test_selection_characterization.py`) pins current choices for
  representative fixtures; every refactor commit keeps it green, and an
  assertion flips only in the commit that deliberately fixes the
  corresponding failure mode, called out in that commit's message.
- Search volume never increases: same query ladders, same early exits, same
  pacing layer (untouched).
- Deliberate improvements to call out when they land: retries prefer the
  same recording from another peer (F1); folder coverage counts everything
  the searches returned (F2); fewer wrong-audio-right-tag assignments (F4);
  the mp3 fallback actually offers mp3s (F5).

### Migration plan

A reviewable series, each commit green under the full suite:

1. Characterization tests for current selection behavior (fixtures for every
   failure mode above, asserting *current* outcomes).
2. F2: merge folder file-lists across queries, keep freshest peer stats.
3. F5: mp3 floor in kbps (after live confirmation of the field's units).
4. `selection.py`: pool + source-groups behind the current API; dedupe →
   grouping; retry order prefers same-recording-next-peer (F1).
5. Joint (track, file) folder assignment + folder-level artist gate (F4).
6. Split match/fetch axes; retire the dead AUTO/PICK semantics; single-track
   and album paths share one candidate-flattening helper (F3).
7. Remaining policy constants move to `selection.py`; downloader consumes
   plans (F6).

## Out of scope

The search pacing/throttle layer (`soulseek/client.py`), tagging and library
layout, Telegram UX, and the audio-source contract itself
(`download_album` / `download_single_track`) — everything upstream stays
source-agnostic and unchanged.
