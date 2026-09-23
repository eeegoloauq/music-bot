# music-bot

Telegram bot: paste a music link → it downloads the audio and drops tagged files into a Navidrome
library. Python 3.12, `uv`, `python-telegram-bot`, `aiohttp`, `mutagen`, slskd (Soulseek daemon).

## Flow

```
bot.py                  URL detect, auth (ALLOWED_USERS), force-mode, dispatch; inline.py = inline search
  → metadata/           any link → Deezer (type, id) → canonical album/track JSON
                        (Tidal/Spotify/Apple scrape + iTunes; Odesli = long-tail fallback)
  → soulseek/           search/enqueue/monitor via slskd; scorer.py ranks, selection.py = all policy
                        (docs/source-selection.md)
  → library/            files.py = path sanitise + tag-based dedup; tagger.py = FLAC/M4A/MP3 tags
  → navidrome.py        library scan after writes
  ⇢ reporting.py        progress events → live-edited status message + final summary
```

The audio source sits behind `download_album` / `download_single_track`; everything upstream is
source-agnostic. Second entry point: **local upload** (`uploads.py` watches `/data/uploads`,
`upload_web.py` optional page, `upload_import.py` identifies by tags) — docs/local-upload-plan.md.
`retagger.py` re-tags an existing library. Planned larger work: `ROADMAP.md`.

## Commands

`uv sync --frozen` · `uv run pytest` (offline: stubbed slskd, no network). No linter configured.
Compose pulls the GHCR image (`MUSICBOT_IMAGE` / `MUSICBOT_TAG`); a local image needs `docker build` first.

## Deploy

Push to `main` or `dev` → Forgejo CI runs tests and publishes the image. The production host pulls
the new image itself (outside this repo) and rolls back if it fails to start. Tag `vX.Y.Z` on `main` →
GitHub Actions builds multi-arch, pushes to GHCR, cuts a Release. `pyproject` version is a
placeholder: the tag is the version.

This repo is public (mirrored to GitHub): no hostnames, IPs or registry addresses in code or docs.

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
- **Force-mode tag wipe**: taggers wipe existing tags except `composer`, `lyricist`, `performer`;
  the peer's `comment` is overwritten by our Deezer URL on purpose.
- **Cover art**: field `cover_uuid` actually holds the full Deezer CDN URL (historical name).
- **Proxy**: every aiohttp session needs `trust_env=True`; a throttled upstream goes to `NO_PROXY`.
- **Quality cap**: `MAX_BIT_DEPTH` / `MAX_SAMPLE_RATE_HZ` (default 24/96000; 16/44100 = redbook).
- **Resume journal**: `/data/pending-downloads.json` (host `./bot-data/`, gitignored — don't
  "clean up") re-issues unfinished downloads on startup; docs/download-resilience.md.
- `slskd` web UI is loopback-only without auth; it mounts the library read-only and downloads to
  `/media/music/.slskd-downloads/`.
