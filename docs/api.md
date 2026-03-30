# hifi-api / Monochrome API Reference

Source: https://github.com/binimum/hifi-api (v2.7, actively maintained fork)
CF Workers rewrite: https://github.com/monochrome-music/hifi-api-workers (hifi.geeked.wtf)

## Instance discovery

Instances are fetched from the Monochrome uptime API (Cloudflare Workers) — the same
source the monochrome.tf frontend uses. Two mirrors for redundancy:

- `https://tidal-uptime.jiffy-puffs-1j.workers.dev/`
- `https://tidal-uptime.props-76styles.workers.dev/`

Response format:
```json
{
  "lastUpdated": "2026-03-30T16:11:04.539Z",
  "api": [{"url": "https://frankfurt-1.monochrome.tf", "version": "2.7"}, ...],
  "streaming": [...],
  "down": [{"url": "https://wolf.qqdl.site", "status": 504, "error": "Track unreachable"}, ...]
}
```

Bot uses `api` list for track/album/search endpoints. `down` list pre-marks dead instances
so they're skipped without wasting time on timeouts.

Fallback: builtin list in `_BUILTIN_INSTANCES` (client.py) if uptime API is unreachable.
Refresh interval: 30 min TTL.

### Instance versions (as of March 2026)

| Instance | Version | Notes |
|---|---|---|
| frankfurt-1, ohio-1, singapore-1.monochrome.tf | 2.7 | Official Monochrome backends |
| hifi.geeked.wtf | 2.7 | CF Workers rewrite |
| eu-central, us-west.monochrome.tf | 2.7 | Official |
| arran.monochrome.tf | 2.6 | Official |
| api.monochrome.tf | 2.5 | Official |
| monochrome-api.samidy.com | 2.3 | Community |
| tidal.kinoplus.online | 2.2 | Community |
| qqdl.site (wolf/maus/vogel/hund/katze) | — | Dead (timeout/504) |
| triton.squid.wtf | — | Dead (502) |

### Why old instances died (March 2026)

Tidal revoked the OAuth client credentials (CLIENT_ID/CLIENT_SECRET) used by hifi-api
v2.2-2.5 instances. All instances sharing those credentials broke simultaneously with
403/upstream errors on track fetches.

The v2.7 fork (binimum) updated to new credentials with multi-credential support and
base64 encoding. Old instances that haven't updated remain dead.

Reference: streamrip PR #932 (March 12, 2026) — same credential update.

### Failover logic (client.py)

- **Dead instances** (`_dead_instances`): connection errors/timeouts → hard skip
- **Soft-failed** (`_soft_failed`): non-200 responses, `{"detail":...}` errors → skip within same album, fresh chance on next album via `clear_soft_failed()`
- If all candidates are soft-failed → clear and retry (prevents lockout between albums)
- Dead/soft-failed sets preserved across instance refresh when the list hasn't changed
- Total timeout: 10s per instance (connect: 4s)

## Key endpoints we use

### GET /album/?id={id}
Returns album metadata + track list.
- `data.cover` → UUID with dashes → `_tidal_cover_url()` converts to CDN URL
- `data.artist.name` → album artist
- `data.items[].item` → track object (disc = `volumeNumber`)

### GET /track/?id={id}&quality=LOSSLESS|HI_RES_LOSSLESS
Returns stream URL or DASH manifest.

**LOSSLESS** (typically 16-bit FLAC, sometimes M4A):
- `manifestMimeType: "application/vnd.tidal.bts"`
- `manifest` → base64 JSON → `{ codecs, urls: ["https://..."] }`
- `codecs: "flac"` → save as `.flac`, tag with mutagen FLAC
- `codecs: "mp4a.40.2"` / `"alac"` → save as `.m4a`, tag with mutagen MP4

**HI_RES_LOSSLESS** (24-bit FLAC via DASH):
- `manifestMimeType: "application/dash+xml"`
- `manifest` → base64 MPD XML
- `assetPresentation` must be `"FULL"` (not `"PREVIEW"` = no Max sub)
- MPD `Representation codecs="flac"` = FLAC-in-M4A containers → remux with ffmpeg

Stream meta fields: `trackReplayGain`, `trackPeakAmplitude`, `albumReplayGain`, `albumPeakAmplitude`, `bitDepth`, `sampleRate`

Note: v2.7 also has `/trackManifests/{id}` (Tidal OpenAPI v2, MPEG-DASH adaptive) but
we use `/track/` which still works and returns direct CDN URLs. No migration needed.

### GET /info/?id={id}
Returns track metadata (same as Tidal public API `GET /v1/tracks/{id}`).
Used by `fetch_single_track()` for single track link downloads.
Response includes `artists[]`, `album.id`, `album.cover`, `duration`, `isrc`, etc.

### GET /search/?s={query}&limit={n}
Search tracks. Returns `{items: [{id, title, artists, album, duration, ...}]}`.

### GET /search/?al={query}&limit={n}
Search via top-hits. Returns `{albums: {items: [...]}, tracks: {items: [...]}, ...}`.
Used for album search in inline mode.

Both search endpoints replaced direct Tidal public API calls (which required TIDAL_TOKEN).
TIDAL_TOKEN has been removed entirely — all API calls go through hifi-api with failover.

### Response envelope
All responses wrapped: `{ "version": "2.7", "data": { ... } }`
Error responses: `{ "detail": "Upstream API error" }` → soft-fail this instance

## Cover art URL
```
https://resources.tidal.com/images/{uuid_with_slashes}/1280x1280.jpg
```
UUID dashes replaced with slashes by `_tidal_cover_url()` in files.py.

## Lyrics (lrclib.net)

```
GET https://lrclib.net/api/get?track_name=...&artist_name=...&album_name=...&duration=...
```

Response fields:
- `plainLyrics` → `lyrics` Vorbis tag (FLAC) / `©lyr` (M4A)
- `syncedLyrics` → `syncedlyrics` Vorbis tag (FLAC) / `----:com.apple.iTunes:SYNCEDLYRICS` (M4A)
- `instrumental: true` → skip, write `lrclibchecked=1` marker instead

Note: hifi-api v2.7 also has `/lyrics/?id={trackId}` (Tidal-native lyrics) but we use
lrclib.net which has better coverage and doesn't depend on instance auth.
