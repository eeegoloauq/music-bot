# Architecture

## Overview

Telegram bot (python-telegram-bot, async long polling) that lets authorized users download albums/tracks
from Tidal (via Monochrome proxy instances) into a self-hosted Navidrome music library.

## Module structure

```
src/
  bot.py          — Telegram handlers, commands, message routing, download orchestration
  inline.py       — inline query handler, now-playing cache, upload dedup, local search
  config.py       — env vars loaded from /data/bot.env via python-dotenv
  navidrome.py    — Subsonic API client (scan, stream, cover art, share, search)
  organizer.py    — organize audio files by tags into Artist/Album/ structure
  tidal/
    __init__.py   — public API: close, fetch_album, fetch_single_track,
                                download_album, download_single_track, ProgressCallback
    client.py     — Monochrome instance manager, aiohttp session, _api_get failover
    metadata.py   — Tidal/Monochrome API fetchers, DASH MPD parser, lrclib
    files.py      — filesystem utils: _sanitize, _find_existing_track, _track_prefix
    tagger.py     — FLAC/M4A tag writing (write + patch)
    downloader.py — download orchestration for albums and single tracks
```

## Dependency graph (no cycles)

```
config
  └─ client (session, instance failover)
       └─ metadata (fetch_album, fetch_track_url, fetch_lyrics)
            └─ tagger (_write_tags, _patch_missing_tags)
                 └─ downloader (download_album, download_single_track)

files (no internal tidal imports) ──────────────────────────────┘ (used by tagger + downloader)
```

## Data flows

### Album download
1. User sends `tidal.com/album/123` or `monochrome.*/album/123`
2. `bot._download_album` → `tidal.fetch_album(id)` via Monochrome API
3. `tidal.download_album` →
   - Phase 1: scan disk, split tracks into existing / to-download
   - Phase 2: `_patch_missing_tags` on existing (parallel lrclib calls)
   - Phase 3: for each missing track:
     - Fire lrclib requests in parallel (all tracks at once)
     - `fetch_track_url` → LOSSLESS (direct FLAC URL) or HI_RES_LOSSLESS (DASH manifest)
     - `_download_flac` or `_download_dash` → `_remux_to_flac` (if DASH)
     - `_write_tags` (FLAC) or `_write_m4a_tags` (M4A, rare)
4. `navidrome.start_scan()` — trigger library index
5. `navidrome.create_share()` — create share link, append to message

### Single track download
Same as album but uses `tidal.fetch_single_track` (hifi-api `/info/` endpoint) to get track
metadata and parent album, then calls `download_single_track`.

### Inline now-playing (audio)
1. User types `@bot` in any chat
2. `navidrome.get_now_playing()` → list of currently playing entries
3. `inline._ensure_cached(entry)` — upload MP3 stream to Telegram, cache file_id
   - `navidrome.stream_song(song_id)` → transcoded MP3 (256/320kbps)
   - `send_audio` → cache `file_id` per song_id
4. Return `InlineQueryResultCachedAudio`

### Inline share
Same but returns `InlineQueryResultArticle` with Navidrome share URL.
Navidrome renders OG preview (cover art) when link is pasted in Telegram.

## Key design decisions

**Why Monochrome proxy instead of direct Tidal API?**
Direct Tidal API requires OAuth + active subscription per token. Monochrome instances have their
own tokens (token.json). We use their `/album/` and `/track/` endpoints. Multiple instances with
automatic failover in case one is down or returns errors.

**Why remux DASH to FLAC?**
HI_RES_LOSSLESS (24-bit) comes as FLAC audio in M4A/MP4 DASH containers. Remuxing with ffmpeg
(copy codec, no re-encode) produces a proper .flac file. Some instances only support HI_RES_LOSSLESS
with a full Tidal Max subscription — `_fetch_hires` tries ALL instances and checks
`assetPresentation == "FULL"` before accepting.

**Why lyrics go in `lyrics` tag (LRC format)?**
Navidrome's mappings.yaml only reads `lyrics` and `unsyncedlyrics` Vorbis tags (not `syncedlyrics`).
It detects synced lyrics by LRC timestamp content `[mm:ss.xx]` in the tag value.
So we write LRC to `lyrics` (Navidrome detects as synced), `syncedlyrics` (foobar2000/Poweramp),
and plain text to `unsyncedlyrics` (fallback). See docs/tagging.md.

**Why asyncio.Semaphore(1) for downloads?**
Tidal CDN throttles parallel downloads per token. Serial downloads are slower but reliable.
lrclib requests fire in parallel (semaphore(10)) since they're cheap and independent.

**Why `_download_flac` writes to `.tmp` first?**
Atomic rename on completion — no partial files visible to Navidrome scanner between retries.
`_download_dash` does the same for the assembled segments file.

## Concurrency patterns

- `_download_semaphore = asyncio.Semaphore(1)` in bot.py — one album/track at a time globally
- `_lrclib_sem = asyncio.Semaphore(10)` in client.py — parallel lrclib requests capped
- `_upload_events: dict[str, asyncio.Event]` in inline.py — dedup parallel inline uploads for same song
- `asyncio.gather(*[_patch_missing_tags(...)])` — parallel lrclib for all existing tracks in album
- `lyrics_tasks = {id: asyncio.create_task(fetch_lyrics(...))}` — prefetch all before downloading

## Error handling

- Per-track failures in `download_album` are caught and added to `failed[]` — album continues
- Monochrome instance failover: tries each in order, skips dead (connection error) and
  soft-failed (non-200/detail error) instances. Soft-failed reset per album.
- Instance discovery via Monochrome uptime API (CF Workers), pre-filters dead instances
- `bootstrap_retries=-1` in bot.py — retries Telegram connection indefinitely on startup
- `_download_flac` retries 3x with exponential backoff on network errors
- Navidrome scan/share failures are non-critical (just omit from user message)
