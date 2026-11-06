# music-bot

Telegram bot for managing a Navidrome music library: import your audio, identify releases,
preview tag changes, browse, share and delete music. Downloads are optional through configured
providers. Python 3.12+, `uv`, `python-telegram-bot`, `aiohttp`, `mutagen`.

## Flow

```
bot.py                  application wiring, startup and shutdown
flows/handlers.py       thin Telegram command and callback adapters
flows/downloads.py      resolution, provider readiness, downloads and resume
flows/uploads.py        upload choices and import orchestration
flows/retag.py          owned preview sessions, selected maintenance and confirmation
flows/catalog.py        album browsing and scoped actions
flows/library_actions.py statistics, guarded deletion, scans and shares
flows/common.py         authorization, cancellable runs, status delivery and backup recovery
flows/state.py          write serialization, duplicate requests and preview invalidation
inline.py               library audio/links, now playing, lyrics, search and delete
uploads.py              stage ZIPs, folders and audio from UPLOAD_DIR
upload_web.py           optional browser intake → same uploads.py pipeline
upload_choices.py       owner chooses original files, identification or manual release
upload_import.py        original import or metadata match → library/importing.py
metadata/               service links/tags → canonical Deezer album/track JSON
providers.py            optional download configuration, health and lazy backend access
  → soulseek/           slskd search/enqueue/monitor; selection.py owns source policy
library/                files.py = formats/identity/dedup; tagger.py = shared tag writers
retagger.py             shared library/album preview and apply; preserves artwork and lyrics
maintenance.py          selected field plans; exact preview values and atomic file replacement
navidrome.py            library API and scan requests after writes
reporting.py            progress events → live status and final summary
```

Base library workflows must start and work without Soulseek, slskd or the optional Python
provider package. Keep backend imports behind `providers.py`. Resolve pasted music links and
check local files before provider readiness; rejected downloads never enter the queue.
The source contract remains `download_album` / `download_single_track`; metadata and library
workflows are source-agnostic. Downloading currently uses Soulseek through slskd; music-service
links provide metadata, not streaming audio extraction.

Current guides: `docs/setup.md`, `docs/usage.md`, `docs/local-upload-plan.md` for import internals,
`docs/providers/` for optional downloads, `docs/source-selection.md` for Soulseek policy.
Larger unfinished work belongs in `ROADMAP.md`.

## Commands

Development checks: `uv sync --frozen --extra soulseek`, then `uv run --no-sync pytest` (offline,
stubbed services, no credentials). The full suite needs the optional extra for backend tests;
the base runtime uses `uv sync --frozen`. No linter configured.
Base `compose.yaml` starts only Music Bot. Optional Soulseek setup uses `compose.soulseek.yaml`
and `.env.soulseek.example`; it selects the `-soulseek` image with the provider package.
Compose pulls an image (`MUSICBOT_IMAGE` / `MUSICBOT_TAG`); build locally before using a local tag.

## Deploy

Push to `main` or `dev` → Forgejo CI runs tests and publishes base and `-soulseek` images. The production host pulls
the new image itself (outside this repo) and rolls back if it fails to start. Tag `vX.Y.Z` on `main` →
GitHub Actions builds multi-arch, pushes to GHCR, cuts a Release. `pyproject` version is a
placeholder: the tag is the version.

This repo is public (mirrored to GitHub): no hostnames, IPs or registry addresses in code or docs.

## Filesystem permissions (mandatory)

Read [docs/volume-permissions.md](docs/volume-permissions.md) before changing library,
upload or maintenance writes.
Create library files through ordinary creation modes, the startup umask and shared directory
inheritance. Use content-only copies; never repair library permissions with runtime chmod/chown,
copy source modes onto new library files or change the process umask inside tasks/threads.
Atomic replacement must respect target write access and explicitly handle inode/ACL/link semantics.
Original imports preserve content bytes; filesystem metadata follows the library access policy.

## Things you'll trip on

- **slskd search lifecycle** (`soulseek/client.py`): responses stay in memory until a search hits
  `Completed`; reading `/responses` mid-search returns empty. `search()` polls for stability, then
  `searches.stop()` (preserves responses), then reads. No global stale-search cleanup before new
  queries — under concurrency it 404s siblings that just finished.
- **Search pacing**: the Soulseek server silently drops search floods (`0 files / 0 peers`) and can
  ban for 30 min. Searches are globally serialized (`SLSKD_SEARCH_MIN_INTERVAL_SECS`, default 10s);
  empty bursts trigger a 90s cooldown. A search that couldn't run raises
  `SearchError`/`SearchThrottledError` — `[]` always means "ran, nothing found", so never
  catch-and-return-empty around `slskd.search()`.
- **Album dedup is tag-based** (`library/files.py::_locate_existing_album`): identity = `comment`
  tag (Deezer album URL we wrote) + `album` tag, not the folder name.
- **Import modes**: keeping originals preserves bytes, tags, artwork, lyrics and filenames with
  no metadata lookup. Identification/manual matching applies release tags through
  `library/importing.py::write_import_tags`, preserving supplied artwork and lyric fields.
  Both modes retain supported sidecars; unfinished files stay in staging. Choices and manual
  release selection belong to the first `ALLOWED_USERS` ID's private chat.
- **Force-mode tag wipe**: low-level taggers wipe tags except `composer`, `lyricist`, `performer`;
  canonical release imports overwrite `comment` with the Deezer URL. The import wrapper restores
  supplied artwork/lyrics; original mode bypasses tag writing. Retag previews preserve both.
- **Retag ownership**: album and whole-library previews share one session, scoped to user/chat.
  Confirm only while its file snapshot still matches; imports, downloads and retags serialize
  writes through the same semaphore. Keep album retags on the shared planner/tag writers.
- **Selected maintenance**: `/maintain` and album **Update selected fields** reuse retagger
  identification, session ownership, snapshots, serialization and apply reporting. New scoped
  writes live in `library/tagger.py`, not another tag writer. Lyrics/artwork fill missing data
  unless replacement is explicitly selected. Preserve plain lyrics when adding synced lyrics,
  add genres without clearing existing values, and keep audio and folder paths stable.
  Preview fetches data without library writes; confirmation applies those exact values without
  network lookups. Check file fingerprints before replacement. Temporary copies have a `.tmp`
  suffix so interrupted writes cannot become library tracks. Artwork replacement updates
  embedded front covers and `cover.jpg`; a missing sidecar alone does not replace embedded art.
- **Upload navigation**: `UPLOAD_HTTP_PORT` enables the browser server. `UPLOAD_WEB_URL` is the
  separate user-reachable LAN/VPN/proxy address for Telegram buttons; never infer it from a
  container hostname or listen port. `/upload` opens the page or local-import setup instructions.
  Keep upload setup under local imports; `docs/providers/` lists download backends.
- **Cover art**: field `cover_uuid` actually holds the full Deezer CDN URL (historical name).
- **Proxy**: every aiohttp session needs `trust_env=True`; a throttled upstream goes to `NO_PROXY`.
- **Quality cap**: `MAX_BIT_DEPTH` / `MAX_SAMPLE_RATE_HZ` (default 24/96000; 16/44100 = redbook).
- **Resume journal**: `/data/pending-downloads.json` (host `./bot-data/`, gitignored — don't
  "clean up") re-issues unfinished downloads only when their provider is ready; otherwise leave
  entries and attempt counters intact. Upload choices persist separately in staging and are
  re-offered on startup. See `docs/download-resilience.md` and `docs/local-upload-plan.md`.
- The optional local `slskd` web UI is loopback-only without auth. Downloads stage under the
  library's `.slskd-downloads/`; library sharing and share rescans require separate opt-ins.
- Browser uploads require `UPLOAD_HTTP_TOKEN` whenever `UPLOAD_HTTP_PORT` is enabled. The
  static entry page is public; all other routes check Bearer auth before parsing or writes.
  Keep the token out of URLs and browser storage. Use HTTPS for remote access; the optional
  Compose mapping binds the host port to loopback. `UPLOAD_WEB_URL` only advertises a link.
