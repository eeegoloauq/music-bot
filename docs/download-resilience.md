# Download resilience across restarts

What happens to in-flight downloads when the bot restarts — deploys recreate
the container, but crashes, OOM kills, and host reboots hit the same path.
This note describes the failure, weighs a deploy gate against a persistent
journal, and documents the journal as implemented (`src/journal.py`, the
runner bracket and `_resume_pending` in `bot.py`, attach-first enqueue in
`soulseek/downloader.py`).

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

## Option B — persistent journal + resume (implemented)

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

### Implementation notes (as landed)

- `src/journal.py`: load/add/remove/bump with atomic rewrites; strictly
  fail-open — persistence is best-effort, the download is the job.
- `bot.py`: the download flows run behind a small `ChatIO` seam (send text
  / photo into a chat) instead of the PTB `Update`, so a journal entry
  drives the same code path as a pasted link. The runners share one
  bracket: accept → `journal.add` → download → report → `journal.remove`.
- **Convergent re-enqueue.** After a restart slskd is often still
  downloading the very file the resumed request picks again, and slskd
  rejects duplicate enqueues. `_ensure_enqueued` attaches to a live
  (non-terminal) transfer row instead of enqueueing, and a refused enqueue
  re-checks for a live row before declaring the peer broken — so the
  duplicate rejection converges to an attach even when it races the first
  check. `wait_for_files` keys on (username, filename), so attached
  transfers report progress like fresh ones.
- **Force is not resumed.** A resumed force re-run would re-stage the
  half-written fresh copy over the original's backup in
  `.redownload-backup`; a plain resume just fills what's missing.
- `compose.yaml`: `- ./bot-data:/data` (pre-create the dir owned by the
  deploy user — docker auto-creates missing bind-mount dirs as root) and
  `stop_grace_period: 30s` so a nearly-finished track can import before
  SIGKILL. Task (2)'s log persistence can reuse the same mount.

### Verification

Offline: journal unit tests + a resume test driving the entry point with a
stubbed bot/downloader. Live (operator): start a 9-track album, restart the
container mid-download, expect the status message to switch to "resuming"
and the album to finish complete, with no duplicate files in the library.
