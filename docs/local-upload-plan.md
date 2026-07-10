# Local upload — design & handoff

> Status: **phases 1–2 implemented** — intake core in `src/uploads.py`, web
> upload page in `src/upload_web.py` (+ tests for both). Phase 3 (tag & file)
> planned. This doc is a self-contained brief so a fresh conversation can pick
> the work up cold. Written on branch `dev`.

## Why this exists

The bot's normal flow downloads audio from Soulseek. Sometimes that's not viable
(a release just isn't on the network, or the only peers are below our quality
bar). For those cases the owner already has the files on their own machine and
just wants to hand them to the bot: **drop an album in, let the bot tag it and
file it into the Navidrome library** — reusing everything downstream of the audio
source (metadata resolve, tagger, dedup, navidrome scan).

This is an **owner-only, optional** feature. It must not add setup friction to
the base bot for people who clone the project — no new required containers, no
new credentials, no extra registration. Disabled = zero surface.

## Architecture: one intake, thin entry points

The core is a **watched intake folder** (`./bot-data/uploads/` on the host,
`/data/uploads` in the container). Everything that lands there — however it got
there — goes through the same intake pipeline: unpack, validate, then (phase 3)
the same tag/dedup/library path the Soulseek downloads use.

Two ways to get a file there:

1. **Web upload page (primary UX, decided)** — a super-lightweight single-page
   site with one button: pick a `.zip`, it streams into the intake folder.
   Details below.
2. **Direct drop (optional, for the docs)** — anyone who prefers can mount the
   folder over Samba / scp a zip in; the watcher doesn't care how the file
   arrived. Costs nothing to support, just needs a README paragraph.

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

## The web upload page

- **Server**: `aiohttp.web` inside the existing bot process — aiohttp is already
  a dependency, so this is **zero new packages**. Started from `_post_init`
  alongside the other background work, only when enabled.
- **Config**: `UPLOAD_HTTP_PORT` env (unset = feature off, default). Compose
  publishes it LAN-only; making it public later is a reverse-proxy concern, not
  a code change — but if it ever goes public it needs auth in front (proxy
  basic-auth is enough; optionally a shared-token check in the handler).
- **Routes**: `GET /` returns one inline HTML page (no build step, no JS
  framework — a file input, a button, a progress bar via `fetch` upload).
  `POST /upload` streams the body to
  `/data/uploads/.incoming/<uuid>.zip` and renames it into `/data/uploads/`
  when complete — the rename is what makes it visible to the watcher, so
  half-received files are never picked up.
- **Limits**: stream to disk (no buffering the album in RAM), enforce a max
  request size, reject non-zip content types politely.
- **Feedback**: the page's job ends at "upload received ✓". Processing status
  goes to **Telegram** (owner already lives there; reuse `LiveStatus` /
  `reporting.py`). A live log view in the browser was considered and parked as
  overkill for the MVP — revisit only if the TG reporting feels lacking in
  practice.

## Scope split

### Phase 1 — intake core (done — `src/uploads.py`)

Just get files from the intake folder in safely and report. **No tagging, no
library placement yet.** Deviations from the sketch below that were made
during implementation: loose audio files dropped into the folder are also
accepted (staged as a folder-of-one); anything unstageable is parked in
`/data/uploads/.rejected/` instead of being re-scanned forever.

- Config: `UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/data/uploads")` in `src/config.py`.
- Ensure `/data/uploads` exists on startup (like the journal's `/data` usage).
- Watch the folder for new arrivals. A simple async poll loop (e.g. every few
  seconds, scan for entries not currently being written) is enough — avoid adding
  a filesystem-watch dependency; poll + a "stable size across two ticks" check to
  know a copy finished (web uploads sidestep this via the rename trick, but
  Samba/scp drops need it). Register the loop from `_post_init` (see
  `_resume_pending` for the pattern of kicking off background work at startup).
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
- Leave extracted files in staging. Do **not** touch `/music` in phase 1.

Keep it a small, reviewable diff. Add offline tests in `tests/` (stub the
filesystem; zero network) covering: happy unpack, zip-slip rejection, size-cap
rejection, non-audio filtering, plain-folder passthrough.

### Phase 2 — web upload endpoint (done — `src/upload_web.py`)

The aiohttp server described above. Separate diff from phase 1 so intake logic
reviews cleanly on its own. Tests: handler-level (aiohttp test utils are part of
aiohttp), streaming write, size-limit rejection, rename-on-complete.

### Phase 3 — tag & file (later, separate task)

Turn a staged upload into a proper library album:
- Identify the release — a fallback ladder, checked against a real sample zip
  (single-track FLAC, 2026-07). Decent rips carry far more than basic tags:
  1. **Source URL in tags** — the sample has `TIDAL_ALBUM_URL` /
     `TIDAL_ALBUM_ID`. Feed that URL straight into the existing resolver
     (`metadata/resolver.py` already handles Tidal links) — identical to the
     owner pasting the link in chat. Zero new resolution code.
  2. **ISRC / UPC tags** — Deezer supports direct lookup
     (`/track/isrc:<isrc>`, `/album/upc:<upc>`); tiny additions to
     `metadata/deezer.py` if step 1's tags are absent.
  3. **`ARTIST` + `ALBUM` text tags** → Deezer search.
  4. **Zip/folder name** as the last resort.
  Whatever rung hits, the result is the same canonical album/track JSON the
  download path uses.
- Retag with the existing taggers (`library/tagger.py`, `retagger.py`) — note the
  force-mode tag-wipe + allow-list behavior documented in `CLAUDE.md`.
- Path-sanitise + dedup via `library/files.py` (tag-based album dedup on the
  Deezer `comment` tag), write into `/music`, trigger a Navidrome scan
  (`navidrome.py`).
- This slots in behind the same source contract as `download_album` /
  `download_single_track` — keep metadata/library/tagger source-agnostic.

Open question for phase 3: when tags/foldername don't cleanly resolve to one
Deezer release, ask the owner in Telegram (inline-button pick among the top
candidates) rather than silently best-guessing — wrong album identity poisons
the tag-based dedup.

### Docs (with phase 2)

README gets a short optional-feature section: enable `UPLOAD_HTTP_PORT`, open
the page, drop a zip; one paragraph noting the intake folder can also be fed
directly (Samba share / scp) since the watcher is delivery-agnostic.

## Branch & CI strategy

Goal: test on the VM from `dev` without any of it showing up in the public GitHub
mirror; only `main` reaches GitHub.

Current wiring (source of truth = Forgejo `homelab/music-bot` at `origin`):
- `dev` tracks `origin/dev` on Forgejo. **Done.** GitHub is only ever written by
  the sync workflow — pushing `dev` cannot leak.
- `.forgejo/workflows/github-sync.yml` — mirrors **main + `v*` tags only** to
  GitHub `eeegoloauq/music-bot`. Verified — no change needed.
- `.forgejo/workflows/deploy.yml` — on push to **main or dev**: test → build
  image → deploy to VM. **Done.** `:latest` stays reserved for main; dev builds
  are tagged `:dev` + `:sha` only, so a test push can't stamp over the image
  tag main deploys from (the deploy step itself is sha-based either way).

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
