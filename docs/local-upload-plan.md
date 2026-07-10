# Local upload — design & handoff

> Status: **planned, not implemented.** This doc is a self-contained brief so a
> fresh conversation can pick the work up cold. Written on branch `dev`.

## Why this exists

The bot's normal flow downloads audio from Soulseek. Sometimes that's not viable
(a release just isn't on the network, or the only peers are below our quality
bar). For those cases the owner already has the files on their own machine and
just wants to hand them to the bot: **drop an album in, let the bot tag it and
file it into the Navidrome library** — reusing everything downstream of the audio
source (metadata resolve, tagger, dedup, navidrome scan).

This is an **owner-only, optional** feature. It must not add setup friction to
the base bot for people who clone the project — no new required containers, no
new credentials, no extra registration.

## The delivery mechanism: a watched folder (decided)

The bot watches `./bot-data/uploads/` on the host (mounted at `/data/uploads`
in the container). The owner copies a `.zip` (or a plain folder of tracks) in;
the bot notices, processes it, and removes/archives it when done.

### Why not "send the zip to the bot in chat"

The cloud Telegram Bot API caps `getFile` (bot-side download) at **20 MB**. A
FLAC album is 200–500 MB, so Telegram simply won't hand the file to the bot.
There is no code workaround — it's a server-side limit at `api.telegram.org`.

### Why not a self-hosted Bot API server

Running the open-source `telegram-bot-api` locally raises the limit to 2 GB and
would make chat upload work. Rejected as overkill for this feature:
- adds a sibling container + requires `api_id`/`api_hash` from my.telegram.org,
- adds onboarding friction for everyone who clones the repo,
- (note for the record: it does **not** expose the host IP — it's outbound-only,
  same as the bot today. The user's concern there was unfounded. It was still
  rejected purely on the friction/weight tradeoff.)

The watched folder solves the actual need (owner's own albums → library) with
zero new deps, zero credentials, and stays fully opt-in.

## Scope split

### Phase 1 — base intake (this task, do first)

Just get files in safely and report. **No tagging, no library placement yet.**

- Config: `UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/data/uploads")` in `src/config.py`.
- Ensure `/data/uploads` exists on startup (like the journal's `/data` usage).
- Watch the folder for new arrivals. A simple async poll loop (e.g. every few
  seconds, scan for entries not currently being written) is enough — avoid adding
  a filesystem-watch dependency; poll + a "stable size across two ticks" check to
  know a copy finished. Register the loop from `_post_init` (see `_resume_pending`
  for the pattern of kicking off background work at startup).
- For each new `.zip`: unpack into a per-upload staging dir under
  `/data/uploads/.extracted/<uuid>/`.
  - **Zip-slip guard**: reject/skip any member whose resolved path escapes the
    target dir (mirror the realpath containment check in
    `bot.py::_handle_delete`).
    - **Size caps**: cap total uncompressed bytes and per-file bytes (reuse the
    spirit of `MAX_FILE_BYTES`); refuse zip bombs.
  - Extract only audio + cover-art members (`.flac .m4a .mp3 .ogg .opus .wav`
    + `.jpg .jpeg .png`); skip the rest.
- For a plain folder dropped in (not a zip): treat it as already-extracted.
- Report back to the owner (there's one owner id in `ALLOWED_USERS`; send via the
  bot). Message = what was found: track count, formats, any skipped members.
  Look at `bot.LiveStatus` / `reporting.py` for how status messages are built.
- Leave extracted files in staging. Do **not** touch `/music` in phase 1.

Keep it a small, reviewable diff. Add offline tests in `tests/` (stub the
filesystem; zero network) covering: happy unpack, zip-slip rejection, size-cap
rejection, non-audio filtering, plain-folder passthrough.

### Phase 2 — tag & file (later, separate task)

Turn a staged upload into a proper library album:
- Identify the release: read existing tags off the files (artist/album), or fall
  back to the folder name; resolve canonical metadata via `metadata/` (Deezer
  resolver → canonical album/track JSON), same as the download path.
- Retag with the existing taggers (`library/tagger.py`, `retagger.py`) — note the
  force-mode tag-wipe + allow-list behavior documented in `CLAUDE.md`.
- Path-sanitise + dedup via `library/files.py` (tag-based album dedup on the
  Deezer `comment` tag), write into `/music`, trigger a Navidrome scan
  (`navidrome.py`).
- This slots in behind the same source contract as `download_album` /
  `download_single_track` — keep metadata/library/tagger source-agnostic.

Open question for phase 2: how to disambiguate when tags/foldername don't
cleanly resolve to one Deezer release (interactive pick vs best-guess).

## Branch & CI strategy (also part of this work)

Goal: test on the VM from `dev` without any of it showing up in the public GitHub
mirror; only `main` reaches GitHub.

Current wiring (source of truth = Forgejo `homelab/music-bot` at `origin`):
- `.forgejo/workflows/deploy.yml` — on push to **main**: test → build image →
  deploy to VM. **Change: also trigger on `dev`** so dev deploys to the VM.
- `.forgejo/workflows/github-sync.yml` — on push to **main** + `v*` tags: mirror
  to GitHub `eeegoloauq/music-bot`. **Already main-only — no change needed.**
  (Verify it still ignores `dev`.)
- `.github/workflows/{tests,release}.yml` run on the GitHub side (tests on main +
  PRs; release on `v*` tags). Untouched.

So: same VM serves both branches — develop on `dev`, deploy `dev` to the VM to
test, then merge `dev` → `main` to promote + publish to GitHub.

Commit hygiene: normal descriptive messages, **no literal "testing X" commits**,
no AI co-author trailers (per `CLAUDE.md`). Don't push without explicit
confirmation.

## Repo facts the implementer needs

- Framework: **python-telegram-bot** (PTB). Handlers registered in
  `src/bot.py::_build_app()`. Auth via the `@authorized` decorator / `ALLOWED_USERS`.
- Background startup work: `_post_init` / `_resume_pending` in `bot.py`.
- Config pattern: env-only, read in `src/config.py` (loads `/data/bot.env`).
- Volumes (`compose.yaml`): `/music` = library, `./bot-data` → `/data` (writable,
  gitignored — the upload staging lives here).
- Tests: `uv run pytest`, offline, stubbed slskd, zero network. CI gates on it.
- Deploy to VM for manual testing: `./deploy-test.sh` hot-reloads `src/*.py`;
  `pyproject.toml`/`Dockerfile` changes need `docker compose up -d --build`.
