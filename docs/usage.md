# Usage

Manage the library from your private Telegram bot. Only users listed in `ALLOWED_USERS` can use it.

## Share music

Enable inline mode in [BotFather](https://t.me/BotFather), then type `@yourbot` in any Telegram chat:

| Query | Action |
|---|---|
| `audio name` | Find a library track, select it, then send its audio |
| `np` | Send the playing track as an audio file |
| `s` | Share a Navidrome link to the playing track |
| `l` | Send the lyrics of the playing track |
| `lib name` | Find a library track or album and share a link |
| `del name` | Delete an album or track, with confirmation |
| `song name` | Find a release on Deezer and check the local library |

The bot sends existing MP3/M4A files when available; other formats are requested as MP3 from
Navidrome. For a chosen track, type `audio name` in the destination chat, select a result,
tap **Send audio here**, then select the audio result. Only the chosen track is uploaded; this
action does not create a share link or send lyrics. Start the bot privately once so it can cache
audio through your private chat. Configure [Navidrome sharing](setup.md#inline-mode-and-share-links) for public links.

## Commands

| Command | Action |
|---|---|
| `/start` | Show shortcut buttons |
| `/help` | Show available commands and inline modes |
| `/scan` | Request a Navidrome library rescan |
| `/stats` | Show artist, album and track counts, and library size |
| `/library name` | Browse local albums and open an album to preview its tag changes |
| `/upload` | Open the configured upload page or view import instructions |
| `/import ID release` | Import a staged upload using an album link or Deezer album ID |
| `/retag` | Preview library-wide tag changes from Deezer and Last.fm |
| `/maintain` | Choose titles, genres, synced lyrics and artwork to update library-wide |
| `/retag confirm` | Apply the pending preview |
| `/retag stop` | Discard the pending preview |
| `/provider` | Check optional download provider status and open setup |
| `/sharescan` | Rescan Soulseek shares when explicitly enabled |

Retagging refreshes track metadata from [Deezer](https://www.deezer.com/) and adds genres from
[Last.fm](https://www.last.fm/api) when an API key is configured. It preserves audio, existing
lyrics and embedded cover art. Use `/maintain` to update those fields separately. A preview expires
after two hours and is invalidated by library changes made through the bot. Confirmation also
checks whether the previewed files changed on disk. Open an album with `/library name`, select
**Preview tag changes**, then confirm or discard. This uses the same planner and tag writers as
`/retag`, scoped to that album; it keeps folder paths stable. A preview belongs to its initiating
user and chat. Only one preview can be pending at a time.

## Update selected fields

Use `/maintain` for the whole library, or open an album with `/library name` and choose
**Update selected fields**. Select any combination of:

- **Titles**: apply track titles from the identified Deezer release.
- **Genres**: add Deezer and optional Last.fm genres, retaining your existing genres.
- **Lyrics**: fill missing synced lyrics from [LRCLIB](https://lrclib.net/), retaining supplied plain text.
- **Artwork**: fill missing embedded covers. A new `cover.jpg` is added when the album has no cover.

Lyrics and artwork cycle through off, **fill missing**, and **replace existing** when tapped.
Replacement updates existing content only after you select that mode and confirm the preview.
Artwork replacement also updates `cover.jpg`; other artwork sidecars are retained.

Choose **Preview changes**, review the selected modes, counts and changes, then **Confirm** or
**Discard**. For large libraries, the preview shows a sample and totals. Unmatched tracks and
unavailable lyrics or covers are reported and skipped. Missing synced lyrics are left alone
when LRCLIB offers only plain text. Genres can still use Last.fm when Deezer cannot identify
the album. No download provider is required.

The same two-hour preview session and user/chat ownership checks apply as for `/retag`.
`/retag confirm` and `/retag stop` also work for these previews. Changed files require a new
preview. Updates use the values fetched during preview; failed writes preserve the original.
FLAC, MP3 and M4A are supported. Audio, filenames,
folders and unrelated tags stay unchanged. On completion, the bot requests a Navidrome scan;
catalogue refresh may finish later.

## Import an album

Album downloads from [Bandcamp](https://get.bandcamp.help/en/articles/15263311-how-do-i-unzip-my-album-download)
arrive as ZIP files. Use `/upload` or **Upload page** in `/start` to open the configured
[browser upload page](setup.md#local-imports). You can also copy an album ZIP or a folder of
tracks into `bot-data/uploads/` on the server.

Both entry points also accept individual audio files, including MP3, M4A and FLAC.
The page transfers your files to the bot; import progress and results are reported in Telegram.
These are ways to supply your own audio and do not require a download provider.

Files sent directly in Telegram chats are not imported. The standard cloud Bot API only lets
bots download files up to [20 MB](https://core.telegram.org/bots/api#getfile). Large ZIP archives
can use the upload page or watched folder instead.

After upload, the owner chooses in Telegram:

- **Keep original files**: import without a metadata lookup. Preserve file bytes, tags, embedded
  artwork, lyrics and filenames. Artist and album tags determine placement; untagged files use
  `Unknown Artist` and the archive name. Existing album directories get a numbered new copy.
- **Identify release**: identify from embedded links, ISRC/UPC, artist and album tags, or the
  archive name, then apply Deezer metadata with optional Last.fm genres and LRCLIB lyrics.
- `/import ID <album link or Deezer ID>`: select a release manually, including after a failed lookup.

The upload message contains the import choices. The owner can reopen them with **Pending uploads**
in the `/start` menu. After a restart, the bot shows unfinished uploads automatically; after a failed
lookup, it shows the choices again. Until an import starts, audio stays in staging. No download provider is required for either mode. All uploads use the same
handler regardless of where the files came from.

Artwork and useful sidecars (PDF booklets, text notes, CUE sheets, logs, LRC lyrics, playlists
and checksum files) travel with the album. Release tagging preserves supplied embedded artwork
and lyric fields. When a sidecar name is already taken, both copies are retained. Original mode
keeps directory structure within the album so relative playlist paths remain valid; release mode
uses canonical audio filenames, so supplied playlists may need their paths adjusted.

Matched tracks are tagged and moved into the library. Existing tracks are kept; redundant uploaded
copies are removed. Unmatched tracks stay in staging. The Cancel button stops the import while
keeping tracks already saved. FLAC, M4A and MP3 support tag writing.

## Downloads

Downloads are optional. The available provider is Soulseek through slskd; follow the
[provider guide](providers/soulseek.md) to enable it. Send an album or track link from
Tidal, Spotify, Apple Music, Deezer, Shazam or YouTube / YouTube Music. Links identify the music;
with that provider enabled, the audio comes from Soulseek.
The bot checks the local library first. Without a configured, available provider, it shows the
identified release and offers import and setup instructions. Send the link again after setup
or recovery; the declined request does not enter the download queue.

Several links queue in order. Add `re` after a link to replace an existing copy; the original is
kept until the replacement succeeds. Downloads prefer lossless files and offer a lossy fallback
when available. The Cancel button stops a task while keeping tracks already saved.

Selection details are in [source selection](source-selection.md).
