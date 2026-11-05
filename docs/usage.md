# Usage

Manage the library from your private Telegram bot. Only users listed in `ALLOWED_USERS` can use it.

## Share music

Enable inline mode in [BotFather](https://t.me/BotFather), then type `@yourbot` in any Telegram chat:

| Query | Action |
|---|---|
| `np` | Send the playing track as an audio file |
| `s` | Share a Navidrome link to the playing track |
| `l` | Send the lyrics of the playing track |
| `lib name` | Find a library track or album and share a link |
| `del name` | Delete an album or track, with confirmation |
| `song name` | Search Deezer for a download |

The bot sends existing MP3/M4A files when available; other formats are requested as MP3 from
Navidrome. Configure [Navidrome sharing](setup.md#inline-mode-and-share-links) for public links.

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

Retagging refreshes track metadata from [Deezer](https://www.deezer.com/) and adds genres from
[Last.fm](https://www.last.fm/api) when an API key is configured. It preserves audio, existing
lyrics and embedded cover art. Synced lyrics from [LRCLIB](https://lrclib.net/) are fetched during
import; library-wide lyrics and artwork updates are on the [roadmap](../ROADMAP.md). A preview expires
after two hours and is invalidated by library changes made through the bot.

## Import an album

Album downloads from [Bandcamp](https://get.bandcamp.help/en/articles/15263311-how-do-i-unzip-my-album-download)
arrive as ZIP files. Drop an album ZIP or a folder of tracks into `bot-data/uploads/`, or use the optional
[browser upload page](setup.md#local-imports).

The bot identifies the release from embedded links, ISRC/UPC, artist and album tags, or the
archive name. Metadata comes from Deezer, with optional Last.fm genre tags and LRCLIB lyrics.
The current importer requires a Deezer match. If no release matches, the files remain in staging
and the bot reports what failed. Importing with the original tags is on the [roadmap](../ROADMAP.md).

Matched tracks are tagged and moved into the library. Existing tracks are kept; redundant uploaded
copies are removed. Unmatched tracks stay in staging. The Cancel button stops the import while
keeping tracks already saved. FLAC, M4A and MP3 support tag writing.

## Downloads

The current download provider is Soulseek through slskd. Send an album or track link from
Tidal, Spotify, Apple Music, Deezer, Shazam or YouTube / YouTube Music. Links identify the music;
the audio comes from Soulseek. See [setup](setup.md).

Several links queue in order. Add `re` after a link to replace an existing copy; the original is
kept until the replacement succeeds. Downloads prefer lossless files and offer a lossy fallback
when available. The Cancel button stops a task while keeping tracks already saved.

Selection details are in [source selection](source-selection.md). Provider separation and
album-specific retagging are planned in the [roadmap](../ROADMAP.md).
