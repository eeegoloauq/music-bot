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
- **One audio extension list.** `library/files.py`, `inline.py`, `uploads.py` and `retagger.py` each
  keep their own and they disagree: uploads put `.wav` into the library, but the inline search,
  album track counts and the delete flow don't see it. Keep one list in `library/files.py` and
  derive the narrower ones (formats we read tags from) from it.
