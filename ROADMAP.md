# Roadmap

Larger work that shouldn't be done in passing. Remove an item when it ships.

- **Animated album artwork downloads.** Optionally fetch matching animated covers and save one
  video per album alongside the audio. Keep static artwork for tags and fallback, select a
  suitable resolution within a configurable size limit, and let imports finish when artwork
  is unavailable. Evaluate source access and reuse terms before choosing a provider.
- **Library maintenance by field.** Add album-level and library-wide actions to fetch missing
  synced lyrics from LRCLIB, refresh genres from Deezer and Last.fm, and update cover art and
  track names from a matched release. Let users choose the fields, review changes and explicitly
  opt into replacing existing lyrics or artwork. Keep bulk lyrics and artwork updates separate
  from the current retag flow, which preserves both.
- **Import purchased albums.** Handle album ZIPs such as Bandcamp downloads without requiring
  a Deezer match. Offer to keep the files' own tags and artwork, or let the user select a release
  manually when identification fails. Preserve supplied artwork and useful sidecar files.
- **Send a selected library track.** Extend audio-file sharing beyond now playing: select a track
  from library search and send it to a Telegram chat. Keep audio files, Navidrome share links and
  lyrics as separate actions.
- **Retag a selected album.** Open an album from the library and preview tag changes for that
  release only, then confirm or discard them. Reuse the existing retag planner and writer.
- **Share links on request.** Keep library search read-only; create a public Navidrome share only
  after the user chooses to share an entry.
- **Scan completion feedback.** Track Navidrome scan status after requesting a scan, report
  completion or failure, and distinguish a scan request from a release appearing in the catalogue.
- **Split `bot.py`** (~1900 lines): Telegram handlers, download orchestration, upload import and
  retag live in one file. Separate by flow, keep handlers thin.
- **One tag writer.** `library/tagger.py` and `retagger.py` both write FLAC/M4A/MP3 tags; `retagger.py`
  (~1100 lines) should reuse the tagger instead of its own copy. `soulseek/downloader.py` (~1000
  lines) is the next candidate.
- **Auth for the upload page.** `upload_web.py` has no auth and binds `0.0.0.0`; it's safe only
  while its port stays unpublished in `compose.yaml`. Add a token or bind to loopback before anyone
  exposes it.
- **One audio extension list.** `library/files.py`, `inline.py`, `uploads.py` and `retagger.py` each
  keep their own and they disagree: uploads put `.wav` into the library, but the inline search,
  album track counts and the delete flow don't see it. Keep one list in `library/files.py` and
  derive the narrower ones (formats we read tags from) from it.
