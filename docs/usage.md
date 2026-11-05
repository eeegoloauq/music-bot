# Usage

Manage the library from your private Telegram bot. Only users listed in `ALLOWED_USERS` can use it.

## Import an album

Drop a ZIP or a folder of tracks into `bot-data/uploads/`, or use the optional
[browser upload page](setup.md#local-imports).

The bot identifies the release from embedded links, ISRC/UPC, artist and album tags, or the
archive name. Metadata comes from Deezer, with optional Last.fm genre tags and LRCLIB lyrics.
If no release matches, the files remain in staging and the bot reports what failed.

Matched tracks are tagged and moved into the library. Existing tracks are kept; redundant uploaded
copies are removed. Unmatched tracks stay in staging. The Cancel button stops the import while
keeping tracks already saved. FLAC, M4A and MP3 support tag writing.

## Commands

| Command | Action |
|---|---|
| `/start` | Show shortcut buttons |
| `/help` | Show available commands and inline modes |
| `/scan` | Request a Navidrome library rescan |
| `/stats` | Show artist, album and track counts, and library size |
| `/retag` | Preview library-wide tag changes from Deezer and Last.fm |
| `/retag confirm` | Apply the pending preview |
| `/retag stop` | Discard the pending preview |
| `/sharescan` | Request a full slskd share rescan |

Retagging updates changed fields and preserves audio and embedded cover art. A preview expires
after two hours and is invalidated by library changes made through the bot.

## Inline mode

Enable inline mode in [BotFather](https://t.me/BotFather), then type `@yourbot` followed by:

| Query | Action |
|---|---|
| `lib name` | Find albums and tracks in Navidrome, with share links when configured |
| `np` | Send the playing track as audio |
| `s` | Send a Navidrome share link for the playing track |
| `l` | Show lyrics for the playing track |
| `del name` | Find an album or track to delete, with confirmation |
| `song name` | Search the external Deezer catalogue for a download |

Use [share-link configuration](setup.md#inline-mode-and-share-links) to enable public Navidrome links.

## Downloads

The current download provider is Soulseek through slskd. Send an album or track link from
Tidal, Spotify, Apple Music, Deezer, Shazam or YouTube / YouTube Music. Links identify the music;
the audio comes from Soulseek. See [setup](setup.md).

Several links queue in order. Add `re` after a link to replace an existing copy; the original is
kept until the replacement succeeds. Downloads prefer lossless files and offer a lossy fallback
when available. The Cancel button stops a task while keeping tracks already saved.

Selection details are in [source selection](source-selection.md). Provider separation and
album-specific retagging are planned in the [roadmap](../ROADMAP.md).
