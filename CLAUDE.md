# CLAUDE.md

High-level guidance for AI assistants and humans working in this repo.

## Read First

Before non-trivial changes:
1. This file (you're here)
2. `README.md`
3. The module-level CLAUDE.md for whichever subsystem you're touching:
   - `src/metadata/CLAUDE.md` — link resolution + Deezer metadata
   - `src/soulseek/CLAUDE.md` — slskd peer search, scoring, downloads
   - `src/tidal/CLAUDE.md` — file/tag helpers + deprecated Monochrome client
4. `.claude/rules/*.md` — deployment, tagging, downloads invariants
5. `docs/ROADMAP.md` — explicit "not now, maybe later" list

## What this is

Telegram bot that ingests music links from supported platforms (Tidal, Spotify, Apple Music, Deezer, YouTube Music, SoundCloud, Shazam, etc.), resolves them to canonical album/track metadata via **Deezer's open API**, downloads the audio from **Soulseek peers via slskd**, tags files with the canonical metadata, and drops them into a local Navidrome library.

## Architecture (April 2026 — post-Monochrome)

```
Telegram message
  ↓
src/bot.py            — URL detection, authorization (ALLOWED_USERS), dispatch
  ↓
src/metadata/         — link resolver + Deezer fetcher (no auth, no token)
  resolver.py           Tidal/Spotify/Apple URL → platform-direct or Odesli → Deezer ID
  deezer.py             api.deezer.com → album/track JSON
  api.py                exposed as `metadata.fetch_album / fetch_lyrics / search / ...`
  ↓
src/soulseek/         — audio downloader via slskd-api
  client.py             search, parse, enqueue, monitor (asyncio.to_thread for sync slskd-api)
  scorer.py             100-pt scoring (coverage 50, quality 10 flat, reliability 25, filename 15)
  matcher.py            album-folder-first, per-track fallback
  downloader.py         orchestrator with retry across candidate peers
  ↓
src/tidal/            — file + tag helpers (kept; module name is historical)
  files.py              path sanitization, _ensure_album_dir (case-insensitive resolve)
  tagger.py             FLAC + M4A tag writers; force-mode wipes peer tags + writes Deezer canonical
  client.py             DEAD Monochrome failover code (kept for git history; nothing live calls it)
  ↓
src/navidrome.py      — triggers Subsonic-style scan after writes
```

**Key import contract:** `bot.py` and `inline.py` do `import metadata as tidal` so existing call sites (`tidal.fetch_album`, `tidal.search`, etc.) route to the Deezer-backed module. `from tidal.files import ...` and `from tidal.tagger import ...` continue resolving to the real `tidal/` package — those modules host pure utility code, not platform-coupled logic. The `import tidal` shape is a transitional alias; `src/tidal/` will eventually be renamed to `src/library/` and the Monochrome remnants deleted (see ROADMAP).

## Sibling containers

- `slskd` — Soulseek daemon (compose service, image `slskd/slskd:latest`)
  - Web UI on `127.0.0.1:5030` (host-loopback only, no auth — internal network)
  - Soulseek peer port `0.0.0.0:50300`
  - Shares `/media/music` read-only; downloads to `/media/music/.slskd-downloads/`
- `navidrome`, `samba` — separate stacks under `/opt/stacks/`

## Engineering rules

- Small, reviewable diffs.
- User-facing behavior stable unless changing it intentionally.
- Configuration & creds via `.env` only — never hardcode.
- No AI co-author trailers on commits unless explicitly requested.
- Casual commit messages (not conventional-commits style).
- Don't push without explicit user confirmation.

## Things you'll trip on

- **Quality cap**: `MAX_BIT_DEPTH` and `MAX_SAMPLE_RATE_HZ` env vars filter Soulseek peers above the cap. Defaults `24` / `96000` cover all reasonable hi-res; `16` / `44100` for redbook-only deployments.
- **slskd quirks** (handled in `soulseek/client.py`): completed searches must be deleted before new ones to avoid silent empty `responses` arrays; explicit `stop()` is required to transition InProgress→Complete and expose responses.
- **Proxy isolation**: `metadata/client.py` uses `trust_env=False` because the bot's `HTTP_PROXY` routes through a SOCKS proxy that Odesli/Deezer throttle. Other sessions (`tidal/client.py`, `navidrome.py`) keep `trust_env=True` for their use cases.
- **Tag normalisation**: in force-mode the FLAC/M4A taggers wipe all existing tags before writing. They preserve a small allow-list (`composer`, `lyricist`, `performer`, peer's `comment` appended after our identifier).
- **Cover art**: stored in album dict as `cover_uuid` (legacy field name) but actually holds the full Deezer CDN URL; `_tidal_cover_url` passes URLs through and resizes via the Deezer URL pattern.

## How to deploy

- `./deploy-test.sh` — hot-reload `src/*.py` via `docker cp` + restart container. Detects local-server vs SSH mode automatically. **Does NOT** rebuild image, so changes to `requirements.txt` or `Dockerfile` need `docker compose up -d --build`.
- New `docker compose up -d --build` requires `--build-arg HTTP_PROXY=` to bypass the host's broken SOCKS-as-HTTP proxy during apt-get.
- After `docker compose up -d music-bot` (recreate), the running container is a fresh image — any prior `docker cp` hot-reloads are lost; resync `src/` files manually or rebuild.

## Next steps deferred

See `docs/ROADMAP.md`. Notable items not yet done: rename `src/tidal/` → `src/library/`, delete `tidal/client.py` + `tidal/metadata.py` Monochrome remnants, MusicBrainz MBID enrichment, peer-side reputation memory, mp3-fallback prompt.
