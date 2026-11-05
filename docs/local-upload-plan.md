# Local imports

Import your own album files through the watched folder or optional browser page. Uploads use
one pipeline regardless of their source. See [usage](usage.md#import-an-album) for the Telegram
choices and [setup](setup.md#local-imports) for configuration.

## Intake

`uploads.py` watches `UPLOAD_DIR` (default `/data/uploads`). The browser page in `upload_web.py`
streams a ZIP or audio file into a hidden incoming directory, then renames it into the watched
folder. Direct drops may also be plain folders. The watcher waits for the size to remain stable
across two polls before staging a drop under `.extracted/<id>/`.

ZIP extraction accepts audio, JPG/PNG/WebP artwork, PDF booklets, text notes, CUE sheets, logs,
LRC lyrics, playlists and checksum files. It enforces per-file and total uncompressed size limits
while streaming. Traversal paths and symlinks are rejected. Folder imports reject symlinks too.
Rejected inputs go to `.rejected/`. Unsupported extras in folder uploads remain recoverable
in staging rather than being discarded after import.

## Import choice

The watcher offers the owner (the first `ALLOWED_USERS` ID) a Telegram prompt through
`upload_choices.py`. The owner chooses original files or release identification. The **Pending
uploads** button in the start menu rebuilds prompts from staging. Unfinished uploads are also
shown automatically at startup, before watching for new arrivals. `/import <id> <album link or Deezer ID>` manually
selects a release. Choices and manual commands are restricted to the owner's private chat.
Concurrent attempts to import the same upload are rejected.

Both modes run through `bot._handle_upload` and `upload_import.py`, sharing cancellation,
serialization with downloads and retagging, progress reports and Navidrome scan requests.

Original mode reads supplied album tags for placement and preserves bytes, filenames, artwork,
lyrics and supported sidecars. No metadata service is called. A name collision creates a
numbered album directory, retaining the existing edition. An upload containing several distinct
album tags is refused; supply one album at a time. Cancellation completes the current file and
leaves remaining files in staging. Retrying original mode continues in the same album directory,
including after a restart. Its destination and archive wrapper are recorded outside the supplied
file tree; sidecars stay in staging until all audio has been filed.

Release mode identifies using embedded service links, UPC, ISRC, artist/album tags and filenames,
or uses the manually supplied release. It writes canonical release tags while preserving supplied
embedded artwork and lyric fields. Matched audio gets canonical filenames and tag-based dedup;
unmatched tracks remain in staging. Supplied artwork and sidecars are copied before staging can
be removed. Conflicting sidecar names receive a content hash suffix. Existing library audio is
retained on a duplicate match.

## Browser page

The page only transfers files; the Telegram prompt controls import. `UPLOAD_HTTP_PORT` enables
the server; `UPLOAD_WEB_URL` supplies the user-reachable URL for Telegram buttons in `/start`,
`/help`, `/upload` and provider fallback messages. The URL may use a LAN/VPN address or a proxy
and does not enable the server itself. Without a URL, the bot links to the local-import setup
instructions. The Compose port remains unpublished by default. The page has no authentication.
Use an authenticated reverse proxy before exposing it outside a trusted network.

Files sent directly in Telegram are not imported. The cloud Bot API download limit is too small
for typical album archives; use the existing page or watched folder.
