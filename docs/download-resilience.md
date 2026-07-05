# Download resilience across restarts

What happens to in-flight downloads when the bot restarts — deploys recreate
the container, but crashes, OOM kills, and host reboots hit the same path.
This note describes the failure, weighs a deploy gate against a persistent
journal, and proposes the journal. Nothing below is implemented yet.

## What breaks today

A deploy recreates only the `music-bot` container. The timeline:

1. `docker compose up -d music-bot` → SIGTERM → the in-flight
   `download_album` coroutine dies mid-await. slskd — a separate container —
   is untouched and **keeps downloading** the enqueued files into staging.
2. The new bot starts with empty in-memory state (`_in_flight`,
   `_download_semaphore`, pending lossy prompts): it has no idea an album
   was in progress.
3. `cleanup_orphan_staging()` runs at startup and deletes staged files that
   have no *active* transfer and are older than 60s. A track that finished
   downloading during the gap has no active transfer — the sweep can throw
   away fully-downloaded bytes that were never imported.
4. The user's Telegram status message is stuck at "Downloading… (3/9)"
   forever. The current workaround is manual: re-paste the link — tag-based
   dedup skips what was imported and re-downloads the rest.

So the bot already *almost* resumes: re-running the same album is cheap and
correct. What's missing is the bot doing it by itself, with the user's
existing message updated instead of silently dying.

## Option A — deploy gate (rejected)

Hold the deploy until active transfers finish: the bot exposes a "busy"
signal (a sentinel file in a shared mount — it has no HTTP server), and the
host-side deploy script polls it with a timeout before recreating the
container.

Rejected because it treats one restart cause, not the failure:

- **Crash-blind.** OOM kills, bugs, and host reboots take the same data
  path and get nothing from a gate.
- **Unbounded deploy latency.** An album takes minutes; a queue of albums
  takes tens of minutes. A gate that times out kills the download anyway —
  right back to today's behavior.
- **Racy.** A user can start a download between the gate check and the
  recreate.
- **Wrong owner.** The gate logic would live in the host's deploy script,
  outside this repo and its CI; the bot can't test or version it.

## Option B — persistent journal + resume (proposed)

Persist the *intent*, not the transfer state — slskd already owns transfer
state, and the library's tag-based dedup already makes re-runs idempotent.

### Journal

One JSON file, `/data/pending-downloads.json`, atomic writes (tmp +
`os.replace`), single writer (the bot). One entry per accepted download
request:

```json
{
  "kind": "album",              // or "track"
  "id": "302127",               // Deezer id
  "chat_id": 123456789,
  "status_message_id": 4242,    // the bot's own progress message
  "force": false,
  "requested_at": "2026-07-05T14:00:00Z",
  "resume_attempts": 0
}
```

Written when a download is accepted (before the semaphore), removed when the
bot reports *any* terminal outcome to the user — success or failure alike.
The journal holds only requests whose outcome the user never saw.

### Resume flow

On startup, after `post_init`:

1. Read the journal; discard unparseable content wholesale (fail open — a
   corrupt journal must never block the bot).
2. For each entry, oldest first: bump `resume_attempts`; if it exceeds 2,
   drop the entry and tell the chat the download was abandoned (a request
   that keeps killing the bot must not become a crash loop).
3. Edit the stored status message ("Bot restarted — resuming…") and re-run
   the normal download flow. Dedup skips imported tracks; searches re-run;
   still-active slskd transfers are re-observed or re-enqueued.

### What v1 deliberately skips

- **Completed-but-unimported staging files are re-downloaded, not
  salvaged.** Importing them without re-download means matching staged
  files back to journal entries and bypassing the search path — real
  complexity for a window of a few minutes. v1 wastes that bandwidth;
  the orphan sweep keeps running unchanged. Possible later optimization.
- **Pending lossy-fallback prompts die with the container.** The inline
  keyboard just stops working; the user re-requests. Rare and cheap.
- **No general task queue.** The journal is a crash ledger, not a
  scheduling system; the in-memory semaphore still serializes downloads.

### Implementation sketch

- `src/journal.py` (~80 lines): load/add/remove/bump with atomic writes —
  pure, unit-testable.
- `bot.py`: the album/track entry points currently take a PTB `Update`;
  resume has none. Extract the small context they actually use (chat id +
  status message handle) so both a fresh Update and a journal entry can
  drive the same flow. This refactor is most of the diff.
- `compose.yaml`: add `- ./bot-data:/data` to the bot service — the repo
  dir on the host persists across recreates. (`config.py` already treats
  `/data` as the config home; task (2)'s log persistence can use the same
  mount later.)
- Optional garnish: `stop_grace_period: 30s` so graceful deploys let a
  nearly-finished track import before SIGKILL.

### Verification

Offline: journal unit tests + a resume test driving the entry point with a
stubbed bot/downloader. Live (operator): start a 9-track album, restart the
container mid-download, expect the status message to switch to "resuming"
and the album to finish complete, with no duplicate files in the library.
