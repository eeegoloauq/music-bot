# Roadmap

Larger work that shouldn't be done in passing. Remove an item when it ships.

- **Split `bot.py`** (~1900 lines): Telegram handlers, download orchestration, upload import and
  retag live in one file. Separate by flow, keep handlers thin.
- **One tag writer.** `library/tagger.py` and `retagger.py` both write FLAC/M4A/MP3 tags; `retagger.py`
  (~1100 lines) should reuse the tagger instead of its own copy. `soulseek/downloader.py` (~1000
  lines) is the next candidate.
- **Auth for the upload page.** `upload_web.py` has no auth and binds `0.0.0.0`; it's safe only
  while its port stays unpublished in `compose.yaml`. Add a token or bind to loopback before anyone
  exposes it.
- **Stale upload docs.** `.env.example` and the `uploads.py` docstring still say uploads only land in
  a temp dir; the bot now identifies and imports them.
