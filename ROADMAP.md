# Roadmap

Larger work that shouldn't be done in passing. Remove an item when it ships.

- **Animated album artwork downloads.** Optionally fetch matching animated covers and save one
  video per album alongside the audio. Keep static artwork for tags and fallback, select a
  suitable resolution within a configurable size limit, and let imports finish when artwork
  is unavailable. Evaluate source access and reuse terms before choosing a provider.
- **Standalone library management.** Make local imports, tagging, metadata lookup and Navidrome
  integration work without a download provider. Move Soulseek dependencies, startup tasks,
  credentials and service dependencies out of the base installation. Keep providers explicitly
  enabled; configure library sharing separately.
- **Provider setup and link handling.** Resolve pasted links and check whether the release is
  already in the library.
  If a download needs a provider and none is configured, show the identified release with
  actions to import files or open provider setup. Distinguish missing configuration from an
  unavailable provider using application configuration and health checks. Provide a dedicated
  Soulseek/slskd setup guide, an optional Compose override and an environment example; support
  connecting to an existing slskd instance. Keep installation snippets in the guide and link
  to it from the bot. Do not queue a download until a provider is available and the user retries.
- **Library-focused introduction and screenshots.** Keep the README and primary screenshots
  aligned with the standalone mode: show local archive import, identification, tagging and
  library browsing with fictional or appropriately licensed demo material. Document Soulseek
  in the optional provider guide and describe the actual capabilities accurately.
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
