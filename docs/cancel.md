# Cancelling a download

Every album download, track download and upload import carries a ✖ Cancel
button on its status message from the first edit ("Fetching album info…",
"Queued — …", "📦 …: identifying release…") until the final one. A resume
after a restart starts a fresh status message with its own button; the old
"🔁 Bot restarted — resuming…" line stays plain. The mp3-fallback prompt has its own Cancel row (acts like
Skip). Only the chat that requested the run can cancel it.

## Mechanism

- Each run is an `ActiveRun` in `bot._active_runs`, keyed by a 12-hex id that
  is the button's `callback_data` (`cancel:<id>`). The run is registered
  before the status message is sent and retired (`close()`) once nothing is
  left to cancel — after the files are on disk, or in the runner's `finally`.
- `RunStatus` wraps the status message: Telegram drops the inline keyboard on
  any edit that does not resend it, so every non-final edit re-attaches the
  button while the run is open; `safe_edit(final=True)` sends
  `reply_markup=None`.
- Downloads run as their own `asyncio.Task` (`_spawn_run`,
  `_await_in_own_task`), never inside the update handler — the update loop
  processes one update at a time, so a download awaited in a handler would
  block the very tap that cancels it.
- The tap handler sets `run.cancel_requested` and calls `task.cancel()`. The
  runner (`_run_album` / `_run_track` / `_handle_upload` /
  `_run_lossy_download`) catches `CancelledError`, and only when
  `cancel_requested` is set treats it as a reported outcome: `uncancel()`,
  restore a force-backup if one was staged (album dir, or the single file a
  force track re-download moved to `*.redownload-backup`; a failed restore
  is said so in the final text), edit the final text ("✖ Cancelled — N/M
  tracks were already saved"), remove the journal entry. A cancel without
  the flag is a shutdown and propagates, so the request stays journaled for
  the next start.
- Container stop: PTB does not wait for these tasks, so a `docker stop`
  behaves like the SIGKILL the journal was designed for — the entry stays,
  slskd keeps the transfer, the next start resumes and attaches to it.

## Where a cancel can land

A cancel lands only at an `await`. The steps that must not be left half-done
are wrapped in `run_to_completion` (`asyncio.shield`, then awaited again on
cancel) so they finish first:

| Step | On cancel |
|---|---|
| Metadata fetch, search, matching, semaphore wait | stops; nothing on disk |
| `_ensure_enqueued` (HTTP request to slskd) | runs to completion, then the transfer is cancelled |
| Transfer in flight (`_await_one_file`) | `slskd.cancel_download(remove=True)` (a refusal is logged — slskd may then finish the file into staging on its own), partial removed from staging, the track's lyrics fetch cancelled |
| Lyrics fetch after a finished transfer | staged file removed (it was never filed) |
| Move into library + tag write | no `await` between them; upload import runs both in one worker-thread step via `run_to_completion` |
| After the last file is on disk (scan, share link, report) | run already closed — button gone, tap answers "already finished" |

Tracks that reached the library stay. Upload staging is left as a failed
import would leave it: the user's files, nothing deleted.
