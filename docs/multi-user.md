# Multi-user: a second person on the bot without a shared library

Status: design note, awaiting sign-off. No code written yet.

## What breaks today

The bot is single-tenant in one specific way: `MUSIC_DIR` is a module-level
constant (`config.py:18`) read directly by `bot.py`, `inline.py` and
`retagger.py` — roughly fifteen call sites. Everything else is already
per-chat: `ALLOWED_USERS` is a list, the resume journal keys entries by
`(kind, id, chat_id)`, and every download already carries the requesting
chat through `ChatIO`.

So adding a Telegram id to `ALLOWED_USERS` works — and lands that person's
downloads in the owner's library, exposes the owner's whole collection to
their inline search (`inline.py:136`), and lets `/file` hand them any file
under the owner's root (the traversal guard at `bot.py:685` is anchored to
the single `MUSIC_DIR`). That is the thing to fix.

## Shape of the change

One idea: **the library root is a function of the requesting Telegram user,
not a constant.**

- Config gains a mapping `tg_id -> library root`, with the owner's root as
  the default. Kept in `bot-data` (it changes when a person is added, not
  when the image is rebuilt), one JSON file, same fail-open discipline as
  the journal.
- The root is resolved once per update, next to `@authorized` — the
  decorator already has `effective_user` in hand — and threaded down as an
  argument. No new global.
- `_collect_stats`, the inline library walk, `/file`, `_locate_existing_album`,
  the import target and the retagger dry-run all take the caller's root.
  The traversal guard is anchored to *that* root: this is the security-
  relevant line of the whole change, not a detail.
- Resume: the journal already stores `chat_id`, so a pending entry resolves
  its own root on startup. One extra lookup, no schema change.
- Staging stays shared. `/music/.slskd-downloads` is slskd's scratch, not a
  library — the import step is what picks a destination root. Keeping one
  staging dir also keeps the slskd contract in `compose.yaml` unchanged.
- Soulseek search is already serialized behind `_search_lock`
  (`soulseek/client.py:305`), so a second person cannot flood the server —
  they queue behind each other. Worth knowing: a guest's album can make the
  owner wait.

## Navidrome side (no code, admin work)

Navidrome 0.63 supports multiple libraries with per-user grants (since
0.58). Libraries are root folders and **may not overlap**, so a guest gets
their own path — e.g. `/media/music-guest` — mounted into both Navidrome
and the bot. The owner is admin and therefore sees every library, with the
sidebar selector to switch; the guest is granted only what they should see.

Two grant models, same code either way:

- guest sees only their own library;
- guest sees their own **and** the common one (read access to the owner's
  collection, writes still land in theirs).

Second model is the recommended default: it is what "a friend on my server"
usually means, and it costs one checkbox.

## Linking a person over Telegram

Secrets travel downward only — the bot never asks anyone to type a password
into a chat.

1. Owner runs `/invite`; the bot returns a one-time code with a short TTL,
   storing only its hash.
2. The newcomer sends `/start <code>`. First presenter wins, attempts are
   capped, failures are silent to the sender and logged.
3. The bot creates a non-admin Navidrome user via the native admin API with
   a generated password, creates their library root, records
   `tg_id -> root, navidrome_user`, and grants library access.
4. It replies with server URL, credentials and a client link, then deletes
   that message after a few minutes. The password is never logged.
5. `/revoke` deactivates the Navidrome user and drops the mapping. Unlinking
   ships with linking, not after it.

Whether the bot keeps the guest's Navidrome password decides one thing
only: whether it can act *as* them in Subsonic (their stars, their
playlists). Subsonic has no impersonation. Default answer is no — it
provisions and forgets, and `bot-data` does not become a store of other
people's passwords.

## Open questions for sign-off

- Grant model: guest-only, or guest + read of the common library?
- Disk: a guest with no cap can fill the pool. Cap per person, or trust plus
  `/stats`?
- Does the guest's music get shared back to Soulseek? Today `slskd` mounts
  the library read-only as the share; a second root is opt-in either way.
