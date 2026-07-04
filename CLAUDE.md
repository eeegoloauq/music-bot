# CLAUDE.md

Telegram bot: paste a music link → it downloads the audio and drops tagged files into a Navidrome library.

## Flow

```
Telegram msg (bot.py)          URL detect, auth (ALLOWED_USERS), force-mode, dispatch
  → metadata/                  any link → Deezer (type, id) → canonical album/track JSON
      resolver.py                Tidal/Spotify/Apple scrape + iTunes; Odesli = long-tail fallback
      deezer.py, client.py       api.deezer.com; one shared aiohttp session (_get_session)
  → soulseek/                  audio via slskd (Soulseek daemon, sibling container)
      client.py                  search / enqueue / monitor — sync slskd-api wrapped in to_thread
      scorer.py                  two-axis: match (duration+name, ×version) / fetch (reliability+quality)
      selection.py               all policy: thresholds, folder plan, quality lock, source-groups
      matcher.py, downloader.py  album-folder-first, per-track fallback, retry across peers
      (docs/source-selection.md = full picture)
  → library/                   files.py = path sanitise + tag-based dedup; tagger.py = FLAC/M4A/MP3
  → navidrome.py               Subsonic scan trigger after writes
```

The audio source sits behind a small contract (`download_album` / `download_single_track`); everything
upstream — metadata, library, tagger, navidrome — is source-agnostic. There's exactly one source
today: `soulseek`.

## Sibling containers (compose.yaml)

- `slskd` — Soulseek daemon. Web UI `127.0.0.1:5030` (loopback, no auth). Peer port `0.0.0.0:50300`.
  Mounts `/media/music` read-only; downloads land in `/media/music/.slskd-downloads/`.
- `navidrome`, `samba` — typical siblings; bot triggers scans over `host.docker.internal`.

## Rules

- Small reviewable diffs. User-facing behavior stable unless changing it on purpose.
- Config & creds via `.env` only, never hardcoded. Lockfile (`uv.lock`) in git; `uv sync --frozen`.
- `uv run pytest` — offline suite in `tests/` (stubbed slskd, zero network). CI gates build/deploy on it.
- Casual commit messages (not conventional-commits). No AI co-author trailers unless asked.
- Don't push without explicit confirmation.

## Things you'll trip on

- **Quality cap** — `MAX_BIT_DEPTH` / `MAX_SAMPLE_RATE_HZ` filter peers above the cap. Defaults
  `24`/`96000` cover all hi-res; use `16`/`44100` for redbook-only.
- **slskd search lifecycle** (`soulseek/client.py`) — peer responses stay in memory until a search
  hits `Completed`; reading `/responses` mid-search returns empty. `search()` polls for stability,
  then `searches.stop()` to force the transition (not a cancel — responses are preserved), then
  reads. Don't run global stale-search cleanup before new queries: under concurrency it 404's
  siblings that just finished.
- **Search pacing** — the Soulseek server silently drops search floods (reads back as `0 files /
  0 peers` for stuff that exists) and can 30-min-ban. All searches are globally serialized with a
  min start interval (`SLSKD_SEARCH_MIN_INTERVAL_SECS`, default 10s). 429s back off and retry; a
  burst of consecutive empty searches triggers a 90s cooldown + re-check of the same query. A
  search that *couldn't run* raises `SearchError`/`SearchThrottledError` — `[]` always means
  "ran, genuinely nothing" — so don't catch-and-return-empty around `slskd.search()`.
- **Album dedup is tag-based, not name-based** (`library/files.py::_locate_existing_album`) —
  identity is the `comment` tag (the Deezer album URL the bot wrote) + `album` tag, not the folder
  name, so folders re-sanitised by other tools still match.
- **Proxy** — every aiohttp session uses `trust_env=True` (aiohttp ignores `HTTP(S)_PROXY` otherwise).
  If a proxy gets throttled by one upstream, add that host to `NO_PROXY` to send it direct.
- **Force-mode tag wipe** — the FLAC/M4A/MP3 taggers wipe existing tags before writing, preserving
  an allow-list (`composer`, `lyricist`, `performer`). The peer's `comment` is NOT preserved — our
  canonical Deezer comment overwrites uniformly (multi-peer assemblies would otherwise show mixed
  rip-source notes in Navidrome).
- **Cover art** — stored as `cover_uuid` (historical name) but actually holds the full Deezer CDN
  URL. `library.files._cover_url` / `metadata.client.cover_url` resize it.

## Deploy

- `./deploy-test.sh` — hot-reload `src/*.py` via `docker cp` + restart. Does NOT rebuild the image,
  so `pyproject.toml` / `Dockerfile` changes need `docker compose up -d --build`.
- After `docker compose up -d music-bot` recreates the container, prior `docker cp` hot-reloads are
  gone — resync `src/` or rebuild.
- **Releases** — tag `vX.Y.Z` on `main`; GitHub Actions builds the multi-arch image, publishes it to
  GHCR (`ghcr.io/eeegoloauq/music-bot`), and cuts a GitHub Release with auto-generated notes.
